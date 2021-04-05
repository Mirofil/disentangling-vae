import imageio
import logging
import os
from timeit import default_timer
from collections import defaultdict
import wandb

from tqdm import trange
import torch
from torch.nn import functional as F

from disvae.utils.modelIO import save_model
from disvae.evaluate import Evaluator

TRAIN_LOSSES_LOGFILE = "train_losses.log"


class Trainer():
    """
    Class to handle training of model.

    Parameters
    ----------
    model: disvae.vae.VAE

    optimizer: torch.optim.Optimizer

    loss_f: disvae.models.BaseLoss
        Loss function.

    device: torch.device, optional
        Device on which to run the code.

    logger: logging.Logger, optional
        Logger.

    save_dir : str, optional
        Directory for saving logs.

    gif_visualizer : viz.Visualizer, optional
        Gif Visualizer that should return samples at every epochs.

    is_progress_bar: bool, optional
        Whether to use a progress bar for training.
    """

    def __init__(self, model, optimizer, loss_f,
                 device='cuda' if torch.cuda.is_available() else 'cpu',
                 logger=logging.getLogger(__name__),
                 save_dir="results",
                 gif_visualizer=None,
                 is_progress_bar=True,
                 metrics_freq=2,
                 seed=None,
                 steps=None,
                 dset_name=None,
                 higgins_drop_slow=None
                 , scheduler = None,
                 sample_size=35,
                 dataset_size=1500,
                 no_shape_classifier=False):

        self.device = device
        self.model = model.to(self.device)
        self.loss_f = loss_f
        self.optimizer = optimizer
        self.save_dir = save_dir
        self.is_progress_bar = is_progress_bar
        self.logger = logger
        self.losses_logger = LossesLogger(os.path.join(self.save_dir, TRAIN_LOSSES_LOGFILE))
        self.gif_visualizer = gif_visualizer
        self.logger.info("Training Device: {}".format(self.device))
        self.metrics_freq = metrics_freq
        self.seed = seed
        self.steps=steps
        self.dset_name=dset_name
        self.higgins_drop_slow = higgins_drop_slow
        self.scheduler = scheduler
        self.sample_size=sample_size
        self.dataset_size=dataset_size
        self.no_shape_classifier=no_shape_classifier

    def __call__(self, data_loader,
                 epochs=10,
                 checkpoint_every=10,
                 wandb_log = False):
        """
        Trains the model.

        Parameters
        ----------
        data_loader: torch.utils.data.DataLoader

        epochs: int, optional
            Number of epochs to train the model for.

        checkpoint_every: int, optional
            Save a checkpoint of the trained model every n epoch.
        """
        start = default_timer()
        storers = []
        self.model.train()

        if wandb_log:
            train_evaluator = Evaluator(model=self.model, loss_f=self.loss_f, device=self.device, dset_name=self.dset_name, seed=self.seed, higgins_drop_slow=self.higgins_drop_slow, 
                sample_size=self.sample_size, dataset_size=self.dataset_size, no_shape_classifier=self.no_shape_classifier)
        
        for epoch in range(epochs):
            storer = defaultdict(list)
            assert self.model.training
            mean_epoch_loss = self._train_epoch(data_loader, storer, epoch)
            self.logger.info('Epoch: {} Average loss per image: {:.2f}'.format(epoch + 1,
                                                                               mean_epoch_loss))
            self.losses_logger.log(epoch, storer)

            if self.gif_visualizer is not None:
                self.gif_visualizer()

            if epoch % checkpoint_every == 0:
                save_model(self.model, self.save_dir,
                           filename="model-{}.pt".format(epoch))
            if self.scheduler is not None:
                self.scheduler.step()
            
            self.model.eval()

            if wandb_log:
                metrics, losses = {}, {}
                if epoch % max(round(epochs/abs(self.metrics_freq)), 10) == 0 and abs(epoch-epochs) >= 5 and (epoch != 0 if self.metrics_freq < 0 else True):
                    metrics = train_evaluator.compute_metrics(data_loader, dataset=self.dset_name)

                losses = train_evaluator.compute_losses(data_loader)
                wandb.log({"epoch":epoch,"metric":metrics, "loss":losses})

            self.model.train()           

        if self.gif_visualizer is not None:
            self.gif_visualizer.save_reset()

        self.model.eval()

        delta_time = (default_timer() - start) / 60
        self.logger.info('Finished training after {:.1f} min.'.format(delta_time))

        if wandb_log:
            return train_evaluator
        else:
            return None

    def _train_epoch(self, data_loader, storer, epoch):
        """
        Trains the model for one epoch.

        Parameters
        ----------
        data_loader: torch.utils.data.DataLoader

        storer: dict
            Dictionary in which to store important variables for vizualisation.

        epoch: int
            Epoch number

        Return
        ------
        mean_epoch_loss: float
            Mean loss per image
        """
        epoch_loss = 0.
        kwargs = dict(desc="Epoch {}".format(epoch + 1), leave=False,
                      disable=not self.is_progress_bar)
        with trange(len(data_loader), **kwargs) as t:
            for step, (data, _) in enumerate(data_loader):
                if self.steps is not None and step > self.steps:
                    break
                iter_loss = self._train_iteration(data, storer)
                epoch_loss += iter_loss

                t.set_postfix(loss=iter_loss)
                t.update()

        mean_epoch_loss = epoch_loss / len(data_loader)
        return mean_epoch_loss

    def _train_iteration(self, data, storer):
        """
        Trains the model for one iteration on a batch of data.

        Parameters
        ----------
        data: torch.Tensor
            A batch of data. Shape : (batch_size, channel, height, width).

        storer: dict
            Dictionary in which to store important variables for vizualisation.
        """
        batch_size, channel, height, width = data.size()
        data = data.to(self.device)

        try:
            recon_batch, latent_dist, latent_sample = self.model(data)
            assert self.model.training
            loss = self.loss_f(data, recon_batch, latent_dist, self.model.training,
                               storer, latent_sample=latent_sample)
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

        except ValueError:
            # for losses that use multiple optimizers (e.g. Factor)
            loss = self.loss_f.call_optimize(data, self.model, self.optimizer, storer)

        return loss.item()


class LossesLogger(object):
    """Class definition for objects to write data to log files in a
    form which is then easy to be plotted.
    """

    def __init__(self, file_path_name):
        """ Create a logger to store information for plotting. """
        if os.path.isfile(file_path_name):
            os.remove(file_path_name)

        self.logger = logging.getLogger("losses_logger")
        self.logger.setLevel(1)  # always store
        file_handler = logging.FileHandler(file_path_name)
        file_handler.setLevel(1)
        self.logger.addHandler(file_handler)

        header = ",".join(["Epoch", "Loss", "Value"])
        self.logger.debug(header)

    def log(self, epoch, losses_storer):
        """Write to the log file """
        for k, v in losses_storer.items():
            log_string = ",".join(str(item) for item in [epoch, k, mean(v)])
            self.logger.debug(log_string)


# HELPERS
def mean(l):
    """Compute the mean of a list"""
    return sum(l) / len(l)
