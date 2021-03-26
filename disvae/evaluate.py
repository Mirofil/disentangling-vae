import os
import logging
import math
import sys
from functools import reduce
from collections import defaultdict
import json
from timeit import default_timer

from tqdm import trange, tqdm
import numpy as np
from numpy.random import RandomState
import torch
from torch import pca_lowrank

from disvae.models.losses import get_loss_f
from disvae.utils.math import log_density_gaussian
from disvae.utils.modelIO import save_metadata
from disvae.models.linear_model import Classifier
from disvae.models.linear_model import weight_reset

from sklearn import decomposition
import wandb

TEST_LOSSES_FILE = "test_losses.log"
METRICS_FILENAME = "metrics.log"
METRIC_HELPERS_FILE = "metric_helpers.pth"


class Evaluator:
    """
    Class to handle training of model.

    Parameters
    ----------
    model: disvae.vae.VAE

    loss_f: disvae.models.BaseLoss
        Loss function.

    device: torch.device, optional
        Device on which to run the code.

    logger: logging.Logger, optional
        Logger.

    save_dir : str, optional
        Directory for saving logs.

    is_progress_bar: bool, optional
        Whether to use a progress bar for training.
    """

    def __init__(self, model, loss_f,
                 device=torch.device("cpu"),
                 logger=logging.getLogger(__name__),
                 save_dir="results",
                 is_progress_bar=True, use_wandb = True):

        self.device = device
        self.loss_f = loss_f
        self.model = model.to(self.device)
        self.logger = logger
        self.save_dir = save_dir
        self.is_progress_bar = is_progress_bar
        self.logger.info("Testing Device: {}".format(self.device))
        self.use_wandb = use_wandb

    def __call__(self, data_loader, is_metrics=False, is_losses=True):
        """Compute all test losses.

        Parameters
        ----------
        data_loader: torch.utils.data.DataLoader

        is_metrics: bool, optional
            Whether to compute and store the disentangling metrics.

        is_losses: bool, optional
            Whether to compute and store the test losses.
        """
        start = default_timer()
        is_still_training = self.model.training
        self.model.eval()

        metric, losses = None, None
        if is_metrics:
            self.logger.info('Computing metrics...')
            metrics = self.compute_metrics(data_loader)
            self.logger.info('Losses: {}'.format(metrics))
            save_metadata(metrics, self.save_dir, filename=METRICS_FILENAME)

        if is_losses:
            self.logger.info('Computing losses...')
            losses = self.compute_losses(data_loader)
            self.logger.info('Losses: {}'.format(losses))
            save_metadata(losses, self.save_dir, filename=TEST_LOSSES_FILE)

        if is_still_training:
            self.model.train()

        self.logger.info('Finished evaluating after {:.1f} min.'.format((default_timer() - start) / 60))

        return metric, losses

    def compute_losses(self, dataloader):
        """Compute all test losses.

        Parameters
        ----------
        data_loader: torch.utils.data.DataLoader
        """
        storer = defaultdict(list)
        for data, _ in tqdm(dataloader, leave=False, disable=not self.is_progress_bar):
            data = data.to(self.device)

            try:
                recon_batch, latent_dist, latent_sample = self.model(data)
                _ = self.loss_f(data, recon_batch, latent_dist, self.model.training,
                                storer, latent_sample=latent_sample)
            except ValueError:
                # for losses that use multiple optimizers (e.g. Factor)
                _ = self.loss_f.call_optimize(data, self.model, None, storer)

            losses = {k: sum(v) / len(dataloader) for k, v in storer.items()}
            return losses

    def compute_metrics(self, dataloader):
        """Compute all the metrics.

        Parameters
        ----------
        data_loader: torch.utils.data.DataLoader
        """

        if self.use_wandb:
             wandb.config["latent_size"] = self.model.latent_dim
             wandb.config["classifier_hidden_size"] = 512
             wandb.config["sample_size"] = 300            
        try:
            lat_sizes = dataloader.dataset.lat_sizes
            lat_names = dataloader.dataset.lat_names
            lat_imgs = dataloader.dataset.imgs
        except AttributeError:
            raise ValueError("Dataset needs to have known true factors of variations to compute the metric. This does not seem to be the case for {}".format(type(dataloader.__dict__["dataset"]).__name__))
        
        self.logger.info("Computing the disentanglement metric")
        accuracies = self._disentanglement_metric(["VAE", "PCA", "ICA"], 300, lat_sizes, lat_imgs, n_epochs=150, dataset_size=1500, hidden_dim=512, use_non_linear=False)
        #sample size is key for VAE, for sample size 50 only 88% accuarcy, compared to 95 for 200 sample sze
        #non_linear_accuracies = self._disentanglement_metric(["VAE", "PCA", "ICA"], 50, lat_sizes, lat_imgs, n_epochs=150, dataset_size=5000, hidden_dim=128, use_non_linear=True) #if hidden dim too large -> no training possible
        if self.use_wandb:
            wandb.log({'VAE_accuracy': accuracies["VAE"], 'PCA_accuracy': accuracies["PCA"], 'ICA_accuracy': accuracies["ICA"]})
            wandb.save("disentanglement_metrics.h5")
        
        self.logger.info("Computing the empirical distribution q(z|x).")
        samples_zCx, params_zCx = self._compute_q_zCx(dataloader)
        len_dataset, latent_dim = samples_zCx.shape

        self.logger.info("Estimating the marginal entropy.")
        # marginal entropy H(z_j)
        H_z = self._estimate_latent_entropies(samples_zCx, params_zCx)

        # conditional entropy H(z|v)
        samples_zCx = samples_zCx.view(*lat_sizes, latent_dim)
        params_zCx = tuple(p.view(*lat_sizes, latent_dim) for p in params_zCx)
        H_zCv = self._estimate_H_zCv(samples_zCx, params_zCx, lat_sizes, lat_names)

        H_z = H_z.cpu()
        H_zCv = H_zCv.cpu()

        # I[z_j;v_k] = E[log \sum_x q(z_j|x)p(x|v_k)] + H[z_j] = - H[z_j|v_k] + H[z_j]
        mut_info = - H_zCv + H_z
        sorted_mut_info = torch.sort(mut_info, dim=1, descending=True)[0].clamp(min=0)

        metric_helpers = {'marginal_entropies': H_z, 'cond_entropies': H_zCv}
        mig = self._mutual_information_gap(sorted_mut_info, lat_sizes, storer=metric_helpers)
        aam = self._axis_aligned_metric(sorted_mut_info, storer=metric_helpers)

        metrics = {'DM': accuracies, 'NLDM': non_linear_accuracies, 'MIG': mig.item(), 'AAM': aam.item()}
        torch.save(metric_helpers, os.path.join(self.save_dir, METRIC_HELPERS_FILE))

        return metrics

    #TODO: experiment with different numbers of latent dimensions for different models
    #maybe plot results of three models for different latent dimensions
    def _disentanglement_metric(self, method_names, sample_size, lat_sizes, imgs, n_epochs=50, dataset_size = 1000, hidden_dim = 256, use_non_linear = False):

        #train models for all concerned methods and stor them in a dict
        methods = {}
        for method_name in method_names:
            if method_name == "VAE":
                methods["VAE"] = self.model

            elif method_name == "PCA":    
                self.logger.info("Training PCA...")
                pca = decomposition.PCA(n_components=self.model.latent_dim, whiten = True)
                imgs_pca = np.reshape(imgs, (imgs.shape[0], imgs.shape[1]**2))
                size = 5000
                if self.use_wandb:
                    wandb.config["PCA_training_size"] = size
                idx = np.random.randint(len(imgs_pca), size = size)
                imgs_pca = imgs_pca[idx, :]       #not enough memory for full dataset -> repeat with random subsets               
                pca.fit(imgs_pca)
                methods["PCA"] = pca
                self.logger.info("Done")

            elif method_name == "ICA":
                self.logger.info("Training ICA...")
                ica = decomposition.FastICA(n_components=self.model.latent_dim)
                imgs_ica = np.reshape(imgs, (imgs.shape[0], imgs.shape[1]**2))
                size = 1000
                if self.use_wandb:
                    wandb.config["ICA_training_size"] = size
                idx = np.random.randint(len(imgs_pca), size = size)
                imgs_ica = imgs_ica[idx, :]       #not enough memory for full dataset -> repeat with random subsets 
                ica.fit(imgs_ica)
                methods["ICA"] = ica
                self.logger.info("Done")

            else: 
                raise ValueError("Unknown method : {}".format(method_name))
        #compute training- and test data for linear classifier      
        data_train =  self._compute_z_b_diff_y(methods, sample_size, lat_sizes, imgs)
        data_test =  self._compute_z_b_diff_y(methods, sample_size, lat_sizes, imgs)
        for method in methods.keys(): 
            data_train[method][0].unsqueeze_(0)
            data_test[method][0].unsqueeze_(0)
       
        #latent dim = length of z_b_diff for arbitrary method = output dimension of linear classifier
        latent_dim = next(iter(data_test.values()))[0].shape[1]

        #generate dataset_size many training data points and 20% of that test data points
        for i in range(dataset_size):
            data = self._compute_z_b_diff_y(methods, sample_size, lat_sizes, imgs)
            for method in methods:
                X_train = data_train[method][0]
                Y_train = data_train[method][1]
                data_train[method] = torch.cat((X_train, data[method][0].unsqueeze_(0)), 0), torch.cat((Y_train, data[method][1]), 0)
            
            if i <= int(dataset_size*0.2):
                
                data = self._compute_z_b_diff_y(methods, sample_size, lat_sizes, imgs)
                for method in methods:
                    X_test = data_test[method][0]
                    Y_test = data_test[method][1]
                    data_test[method] = torch.cat((X_test, data[method][0].unsqueeze_(0)), 0), torch.cat((Y_test, data[method][1]), 0)

        model = Classifier(latent_dim,hidden_dim,len(lat_sizes), use_non_linear)
            
        model.to(self.device)
        model.train()

        #log softmax with NLL loss 
        criterion = torch.nn.NLLLoss()
        optim = torch.optim.Adam(model.parameters(), lr=0.01)
       
        test_acc = {}
        for method in methods.keys():
            print(f'Training the classifier for model {method}')
            for e in range(n_epochs):
                optim.zero_grad()
                
                X_train, Y_train = data_train[method]
                X_train = X_train.to(self.device)
                Y_train = Y_train.to(self.device)
                X_test , Y_test = data_test[method]
                X_test = X_test.to(self.device)
                Y_test = Y_test.to(self.device)

                
                scores_train = model(X_train)   
                loss = criterion(scores_train, Y_train)
                loss.backward()
                optim.step()
                
                if (e+1) % 10 == 0:
                    scores_test = model(X_test)   
                    test_loss = criterion(scores_test, Y_test)
                    print(f'In this epoch {e+1}/{n_epochs}, Training loss: {loss.item():.4f}, Test loss: {test_loss.item():.4f}')
            
            model.eval()
            with torch.no_grad():
                
                scores_train = model(X_train)
                scores_test = model(X_test)
                _, prediction_train = scores_train.max(1)
                _, prediction_test = scores_test.max(1)

                train_acc = (prediction_train==Y_train).sum().float()/len(X_train)
                test_acc[method] = (prediction_test==Y_test).sum().float()/len(X_test)
                print(f'Accuracy of {method} on training set: {train_acc.item():.4f}, test set: {test_acc[method].item():.4f}')
                
            model.apply(weight_reset)

        return test_acc

    def _compute_z_b_diff_y(self, methods, sample_size, lat_sizes, imgs):
        """
        Compute the disentanglement metric score as proposed in the original paper
        reference: https://github.com/deepmind/dsprites-dataset/blob/master/dsprites_reloading_example.ipynb
        """
        imgs_sampled1, imgs_sampled2, y = self._images_from_data_gen(sample_size, lat_sizes, imgs)

        res = {}
        #calculate the expectation values of the normal distributions in the latent representation for the given images
        for method in methods.keys():
            if method == "VAE":
                with torch.no_grad():
                    mu1, _ = self.model.encoder(imgs_sampled1.to(self.device))
                    mu2, _ = self.model.encoder(imgs_sampled2.to(self.device))  
            elif method == "PCA":
                pca = methods[method]
                #flatten images
                imgs_sampled_pca1 = torch.reshape(imgs_sampled1, (imgs_sampled1.shape[0], imgs_sampled1.shape[2]**2))
                imgs_sampled_pca2 = torch.reshape(imgs_sampled2, (imgs_sampled2.shape[0], imgs_sampled2.shape[2]**2))
                
                mu1 = torch.from_numpy(pca.transform(imgs_sampled_pca1)).float()
                mu2 = torch.from_numpy(pca.transform(imgs_sampled_pca2)).float()

            elif method == "ICA":
                ica = methods[method]
                #flatten images
                imgs_sampled_ica1 = torch.reshape(imgs_sampled1, (imgs_sampled1.shape[0], imgs_sampled1.shape[2]**2))
                imgs_sampled_ica2 = torch.reshape(imgs_sampled2, (imgs_sampled2.shape[0], imgs_sampled2.shape[2]**2))
                
                mu1 = torch.from_numpy(ica.transform(imgs_sampled_ica1)).float()
                mu2 = torch.from_numpy(ica.transform(imgs_sampled_ica2)).float()
                
            else: 
                raise ValueError("Unknown method : {}".format(method)) 

            z_diff = torch.abs(torch.sub(mu1, mu2))
            z_diff_b = torch.mean(z_diff, 0)
            res[method] = z_diff_b, torch.from_numpy(y)

        return res

    def _images_from_data_gen(self, sample_size, lat_sizes, imgs):

        #sample random latent factor that is to be kept fixed
        y = np.random.randint(lat_sizes.size, size=1)
        y_lat = np.random.randint(lat_sizes[y], size=sample_size)

        #sample to sets of data generative factors such that the yth value is the same accross the two sets
        samples1 = np.zeros((sample_size, lat_sizes.size))
        samples2 = np.zeros((sample_size, lat_sizes.size))

        for i, lat_size in enumerate(lat_sizes):
            samples1[:, i] = y_lat if i == y else np.random.randint(lat_size, size=sample_size) 
            samples2[:, i] = y_lat if i == y else np.random.randint(lat_size, size=sample_size)

        latents_bases = np.concatenate((lat_sizes[::-1].cumprod()[::-1][1:],
                                np.array([1,])))

        latent_indices1 = np.dot(samples1, latents_bases).astype(int)
        latent_indices2 = np.dot(samples2, latents_bases).astype(int)

        #use the data generative factors to simulate two sets of images from the dataset
        imgs_sampled1 = torch.from_numpy(imgs[latent_indices1]).unsqueeze_(1).float()
        imgs_sampled2 = torch.from_numpy(imgs[latent_indices2]).unsqueeze_(1).float()

        return imgs_sampled1, imgs_sampled2, y


    def _mutual_information_gap(self, sorted_mut_info, lat_sizes, storer=None):
        """Compute the mutual information gap as in [1].

        References
        ----------
           [1] Chen, Tian Qi, et al. "Isolating sources of disentanglement in variational
           autoencoders." Advances in Neural Information Processing Systems. 2018.
        """
        # difference between the largest and second largest mutual info
        delta_mut_info = sorted_mut_info[:, 0] - sorted_mut_info[:, 1]
        # NOTE: currently only works if balanced dataset for every factor of variation
        # then H(v_k) = - |V_k|/|V_k| log(1/|V_k|) = log(|V_k|)
        H_v = torch.from_numpy(lat_sizes).float().log()
        mig_k = delta_mut_info / H_v
        mig = mig_k.mean()  # mean over factor of variations

        if storer is not None:
            storer["mig_k"] = mig_k
            storer["mig"] = mig

        return mig

    def _axis_aligned_metric(self, sorted_mut_info, storer=None):
        """Compute the proposed axis aligned metrics."""
        numerator = (sorted_mut_info[:, 0] - sorted_mut_info[:, 1:].sum(dim=1)).clamp(min=0)
        aam_k = numerator / sorted_mut_info[:, 0]
        aam_k[torch.isnan(aam_k)] = 0
        aam = aam_k.mean()  # mean over factor of variations

        if storer is not None:
            storer["aam_k"] = aam_k
            storer["aam"] = aam

        return aam

    def _compute_q_zCx(self, dataloader):
        """Compute the empiricall disitribution of q(z|x).

        Parameter
        ---------
        dataloader: torch.utils.data.DataLoader
            Batch data iterator.

        Return
        ------
        samples_zCx: torch.tensor
            Tensor of shape (len_dataset, latent_dim) containing a sample of
            q(z|x) for every x in the dataset.

        params_zCX: tuple of torch.Tensor
            Sufficient statistics q(z|x) for each training example. E.g. for
            gaussian (mean, log_var) each of shape : (len_dataset, latent_dim).
        """
        len_dataset = len(dataloader.dataset)
        latent_dim = self.model.latent_dim
        n_suff_stat = 2

        q_zCx = torch.zeros(len_dataset, latent_dim, n_suff_stat, device=self.device)

        n = 0
        with torch.no_grad():
            for x, label in dataloader:
                batch_size = x.size(0)
                idcs = slice(n, n + batch_size)
                q_zCx[idcs, :, 0], q_zCx[idcs, :, 1] = self.model.encoder(x.to(self.device))
                n += batch_size

        params_zCX = q_zCx.unbind(-1)
        samples_zCx = self.model.reparameterize(*params_zCX)

        return samples_zCx, params_zCX

    def _estimate_latent_entropies(self, samples_zCx, params_zCX,
                                   n_samples=10000):
        r"""Estimate :math:`H(z_j) = E_{q(z_j)} [-log q(z_j)] = E_{p(x)} E_{q(z_j|x)} [-log q(z_j)]`
        using the emperical distribution of :math:`p(x)`.

        Note
        ----
        - the expectation over the emperical distributio is: :math:`q(z) = 1/N sum_{n=1}^N q(z|x_n)`.
        - we assume that q(z|x) is factorial i.e. :math:`q(z|x) = \prod_j q(z_j|x)`.
        - computes numerically stable NLL: :math:`- log q(z) = log N - logsumexp_n=1^N log q(z|x_n)`.

        Parameters
        ----------
        samples_zCx: torch.tensor
            Tensor of shape (len_dataset, latent_dim) containing a sample of
            q(z|x) for every x in the dataset.

        params_zCX: tuple of torch.Tensor
            Sufficient statistics q(z|x) for each training example. E.g. for
            gaussian (mean, log_var) each of shape : (len_dataset, latent_dim).

        n_samples: int, optional
            Number of samples to use to estimate the entropies.

        Return
        ------
        H_z: torch.Tensor
            Tensor of shape (latent_dim) containing the marginal entropies H(z_j)
        """
        len_dataset, latent_dim = samples_zCx.shape
        device = samples_zCx.device
        H_z = torch.zeros(latent_dim, device=device)

        # sample from p(x)
        samples_x = torch.randperm(len_dataset, device=device)[:n_samples]
        # sample from p(z|x)
        samples_zCx = samples_zCx.index_select(0, samples_x).view(latent_dim, n_samples)

        mini_batch_size = 10
        samples_zCx = samples_zCx.expand(len_dataset, latent_dim, n_samples)
        mean = params_zCX[0].unsqueeze(-1).expand(len_dataset, latent_dim, n_samples)
        log_var = params_zCX[1].unsqueeze(-1).expand(len_dataset, latent_dim, n_samples)
        log_N = math.log(len_dataset)
        with trange(n_samples, leave=False, disable=self.is_progress_bar) as t:
            for k in range(0, n_samples, mini_batch_size):
                # log q(z_j|x) for n_samples
                idcs = slice(k, k + mini_batch_size)
                log_q_zCx = log_density_gaussian(samples_zCx[..., idcs],
                                                 mean[..., idcs],
                                                 log_var[..., idcs])
                # numerically stable log q(z_j) for n_samples:
                # log q(z_j) = -log N + logsumexp_{n=1}^N log q(z_j|x_n)
                # As we don't know q(z) we appoximate it with the monte carlo
                # expectation of q(z_j|x_n) over x. => fix a single z and look at
                # proba for every x to generate it. n_samples is not used here !
                log_q_z = -log_N + torch.logsumexp(log_q_zCx, dim=0, keepdim=False)
                # H(z_j) = E_{z_j}[- log q(z_j)]
                # mean over n_samples (i.e. dimesnion 1 because already summed over 0).
                H_z += (-log_q_z).sum(1)

                t.update(mini_batch_size)

        H_z /= n_samples

        return H_z

    def _estimate_H_zCv(self, samples_zCx, params_zCx, lat_sizes, lat_names):
        """Estimate conditional entropies :math:`H[z|v]`."""
        latent_dim = samples_zCx.size(-1)
        len_dataset = reduce((lambda x, y: x * y), lat_sizes)
        H_zCv = torch.zeros(len(lat_sizes), latent_dim, device=self.device)
        for i_fac_var, (lat_size, lat_name) in enumerate(zip(lat_sizes, lat_names)):
            idcs = [slice(None)] * len(lat_sizes)
            for i in range(lat_size):
                self.logger.info("Estimating conditional entropies for the {}th value of {}.".format(i, lat_name))
                idcs[i_fac_var] = i
                # samples from q(z,x|v)
                samples_zxCv = samples_zCx[idcs].contiguous().view(len_dataset // lat_size,
                                                                   latent_dim)
                params_zxCv = tuple(p[idcs].contiguous().view(len_dataset // lat_size, latent_dim)
                                    for p in params_zCx)

                H_zCv[i_fac_var] += self._estimate_latent_entropies(samples_zxCv, params_zxCv
                                                                    ) / lat_size
        return H_zCv
