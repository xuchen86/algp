import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import gpytorch
from gpytorch.kernels import RBFKernel, WhiteNoiseKernel, MaternKernel, SpectralMixtureKernel, ScaleKernel
from gpytorch.means import ZeroMean
from gpytorch.likelihoods import GaussianLikelihood
from gpytorch.distributions import MultivariateNormal

from utils import to_torch, to_numpy
# import ipdb


class IdentityLatentFunction(nn.Module):
    def __init__(self):
        super(IdentityLatentFunction, self).__init__()
        self.embed_dim = None

    def forward(self, x):
        return x


class LinearLatentFunction(nn.Module):
    def __init__(self, input_dim, embed_dim):
        super(LinearLatentFunction, self).__init__()
        self.fc = nn.Linear(input_dim, embed_dim)
        self.embed_dim = embed_dim

    def forward(self, x):
        return self.fc(x)


class NonLinearLatentFunction(nn.Module):
    def __init__(self, input_dim, f1_dim, embed_dim):
        super(NonLinearLatentFunction, self).__init__()
        self.fc1 = nn.Linear(input_dim, f1_dim, bias=False)
        self.fc2 = nn.Linear(f1_dim, embed_dim, bias=False)
        self.embed_dim = embed_dim

    def forward(self, inp):
        x = F.tanh(self.fc1(inp))
        x = self.fc2(x)
        return x


# class FieldLatentFunction(nn.Module):
#     def __init__(self, spatial_dim, gene_dim):
#         super(FieldLatentFunction, self).__init__()
#         self.spatial_dim = spatial_dim
#         self.gene_dim = gene_dim
#         f1_dim = 3
#         f2_dim = 3
#         # NOTE: it may not be a good idea to perform transformation of rr dimensions
#         self.rr_fc = nn.Linear(self.spatial_dim, f1_dim)
#         self.v_fc = nn.Linear(self.gene_dim, f1_dim)
#         self.fc = nn.Linear(2*f1_dim, f2_dim)
#         # self.apply(weights_init)

#     def forward(self, inp):
#         # TODO: perhaps too many parameters (might overfit)
#         inp1, inp2 = torch.split(inp, [self.spatial_dim, self.gene_dim], dim=-1)
#         x1 = F.relu(self.rr_fc(inp1))
#         x2 = F.relu(self.v_fc(inp2))
#         x = torch.cat([x1, x2], dim=-1)
#         x = self.fc(x)
#         return x


# class FieldLatentFunction(nn.Module):
#     def __init__(self, spatial_dim, gene_dim, f1_dim):
#         super(FieldLatentFunction, self).__init__()
#         self.spatial_dim = spatial_dim
#         self.gene_dim = gene_dim
#         self.embed_dim = self.spatial_dim + 1
#         self.fc1 = nn.Linear(self.gene_dim, f1_dim, bias=False)
#         self.fc2 = nn.Linear(f1_dim, 1, bias=False)
        
#     def forward(self, inp):
#         inp1, inp2 = torch.split(inp, [self.spatial_dim, self.gene_dim], dim=-1)
#         x = self.fc(inp2)
#         x = torch.cat([inp1, x], dim=-1)
#         return x


class GPR(object):
    def __init__(self, latent=None, lr=.01, max_iterations=200, kernel_params=None, latent_params=None, learn_likelihood_noise=True):
        self._train_x = None
        self._train_y = None
        self._train_y_mean = None
        self._train_var = None
        self.likelihood = None
        self.model = None
        self.optimizer = None
        self.mll = None
        self.lr = lr
        self.latent = latent
        self.kernel_params = kernel_params
        self.latent_params = latent_params
        self.max_iter = max_iterations
        self.learn_likelihood_noise = learn_likelihood_noise

    @property
    def train_x(self):
        return self._train_x.cpu().numpy()

    @property
    def train_y(self):
        return self._train_y.cpu().numpy()

    @property
    def train_var(self):
        if self._train_var is None:
            return None
        return self._train_var.cpu().numpy()

    def reset(self, x, y, var):
        self.set_train_data(x, y, var)
        # self.likelihood = GaussianLikelihood(learn_noise=self.learn_likelihood_noise)
        self.likelihood = GaussianLikelihood()
        self.model = ExactGPModel(self._train_x, self._zero_mean_train_y, self.likelihood, self._train_var, self.latent, self.kernel_params, self.latent_params)
        self.optimizer = torch.optim.Adam([{'params': self.model.parameters()}, ], lr=self.lr)
        self.mll = gpytorch.mlls.ExactMarginalLogLikelihood(self.likelihood, self.model)
        self.lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, mode='min', patience=50, verbose=True)
        
    def set_train_data(self, x, y, var=None):
        self._train_x = to_torch(x)
        self._train_y = to_torch(y)
        self._train_y_mean = self._train_y.mean()      
        self._zero_mean_train_y = self._train_y - self._train_y_mean
        if var is not None:
            self._train_var = to_torch(var)

        if self.model is not None:
            self.model.set_train_data(inputs=self._train_x, targets=self._zero_mean_train_y, strict=False)

    def fit(self, x, y, var=None, disp=False):
        if var is None:
            var = np.full(len(y), 1e-5)
        self.reset(x, y, var)
        self.model.train()
        self.likelihood.train()
        
        losses = []
        for i in range(self.max_iter):
            self.optimizer.zero_grad()
            output = self.model(self._train_x)
            loss = -self.mll(output, self._zero_mean_train_y)
            loss.backward()
            self.optimizer.step()
            self.lr_scheduler.step(loss)
            if disp:
                print(i, loss.item())
            if i == 0:
                initial_ll = -loss.item()
            elif i == self.max_iter - 1:
                final_ll = -loss.item()
            losses.append(loss.item())
        print('Initial LogLikelihood {:.3f} Final LogLikelihood {:.3f}'.format(initial_ll, final_ll))
        
    def cov_mat(self, x1, x2=None, white_noise_var=None, add_likelihood_var=False):
        # white_noise_var needs to be passed explicitly
        x1_ = to_torch(x1)
        x2_ = to_torch(x2)
        
        self.model.eval()
        with torch.no_grad():
            x1_ = self.model.latent_func(x1_)
            if x2_ is None or torch.equal(x1_, x2_):
                cov = self.model.kernel_covar_module(x1_).evaluate().cpu().numpy()
            else:
                x2_ = self.model.latent_func(x2_)
                cov = self.model.kernel_covar_module(x1_, x2_).evaluate().cpu().numpy()

            if white_noise_var is not None:
                cov += np.diag(white_noise_var)
            
            # for training data, add likelihood variance
            if add_likelihood_var:
                cov += self.likelihood.log_noise.exp().item() * np.eye(len(cov))
        return cov

    def predict(self, x, return_cov=False, return_std=False):
        # returns posterior distribution conditioned on training data
        # call set_train_data method to set a different training data
        self.model.eval()
        self.likelihood.eval()
        x_ = to_torch(x)

        with torch.no_grad():
            pred = self.likelihood(self.model(x_))
            pred_mean = (pred.mean() + self._train_y_mean).cpu().numpy()
            if return_std:
                return pred_mean, pred.covar().diag().cpu().numpy()
            elif return_cov:
                return pred_mean, pred.covar().evaluate().cpu().numpy()
            return pred_mean

    def get_embeddings(self, x):
        with torch.no_grad():
            x_ = to_torch(x)
            embeds = self.model.latent_func(x_)
            return to_numpy(embeds)


class ExactGPModel(gpytorch.models.ExactGP):
    def __init__(self, train_x, train_y, likelihood, var=None, latent=None, kernel_params=None, latent_params=None):
        super(ExactGPModel, self).__init__(train_x, train_y, likelihood)
        if latent_params is None:
            latent_params = {'input_dim': train_x.size(-1)}
        self._set_latent_function(latent, latent_params)
        
        self.mean_module = ZeroMean()
        ard_num_dims = self.latent_func.embed_dim if self.latent_func.embed_dim is not None else train_x.size(-1)
        
        kernel = kernel_params['type'] if kernel_params is not None else 'rbf'
        if kernel is None or kernel == 'rbf':
            self.kernel_covar_module = ScaleKernel(RBFKernel(ard_num_dims=ard_num_dims))
        elif kernel == 'matern':
            self.kernel_covar_module = ScaleKernel(MaternKernel(nu=1.5, ard_num_dims=ard_num_dims))
            # without scale kernel: very poor performance
            # matern 0.5, 1.5 and 2.5 all have similar performance
        elif kernel == 'spectral_mixture':
            self.kernel_covar_module = SpectralMixtureKernel(num_mixtures=kernel_params['n_mixtures'], ard_num_dims=train_x.size(-1))
            self.kernel_covar_module.initialize_from_data(train_x, train_y)
        else:
            raise NotImplementedError

        # set covariance module
        if var is not None:
            self.noise_covar_module = WhiteNoiseKernel(var)
            self.covar_module = self.kernel_covar_module + self.noise_covar_module
        else:
            self.covar_module = self.kernel_covar_module
        
    def _set_latent_function(self, latent, latent_params):
        if latent is None or latent == 'identity':
            self.latent_func = IdentityLatentFunction()
        elif latent == 'linear':
            if 'embed_dim' not in latent_params:
                latent_params['embed_dim'] = 6
            self.latent_func = LinearLatentFunction(latent_params['input_dim'], latent_params['embed_dim'])
        elif latent == 'non_linear':
            if 'embed_dim' not in latent_params:
                latent_params['embed_dim'] = 6
            self.latent_func = NonLinearLatentFunction(latent_params['input_dim'], latent_params['embed_dim'], latent_params['embed_dim'])
        else:
            raise NotImplementedError

    def forward(self, inp):
        x = self.latent_func(inp)
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return MultivariateNormal(mean_x, covar_x)