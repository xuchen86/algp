from sklearn import gaussian_process
from sklearn.gaussian_process.kernels import RBF, Matern

import torch
import torch.nn as nn
import torch.nn.functional as F
import gpytorch
from gpytorch.kernels import RBFKernel, WhiteNoiseKernel, MaternKernel
from gpytorch.means import ZeroMean
from gpytorch.likelihoods import GaussianLikelihood
from gpytorch.random_variables import GaussianRandomVariable

import numpy as np
from scipy.linalg import cholesky, cho_solve, solve_triangular
import warnings
import ipdb
from utils import to_torch
from pprint import pprint


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1 or classname.find('Linear') != -1:
        nn.init.orthogonal_(m.weight.data)
        if m.bias is not None:
            m.bias.data.fill_(0)


class SklearnGPR(object):
    def __init__(self, kernel=None):
        if kernel == 'rbf' or kernel is None:
            self.init_kernel = RBF(length_scale=1.0)
        elif kernel == 'matern':
            self.init_kernel = Matern(nu=1.5, length_scale=1.0)
        else:
            raise NotImplementedError
        self.model = None

    @property
    def train_x(self):
        return self.model.X_train_

    @property
    def train_var(self):
        return self.model.alpha

    @property
    def kernel_(self):
        return self.model.kernel_
        
    def set_train_var(self, var):
        self.model.alpha = var

    def fit(self, x, y, var=None):
        self.reset()
        self.set_train_var(var if var is not None else 1e-10)
        self.model.fit(x, y)

    def predict(self, x, return_std=False, return_cov=False):
        assert not (return_std and return_cov), 'Can return either std or var'
        return self.model.predict(x, return_std=return_std, return_cov=return_cov)

    def reset(self):
        self.model = gaussian_process.GaussianProcessRegressor(self.init_kernel)

    def cov_mat(self, x1, x2, white_noise=None):
        cov = self.model.kernel_(x1, x2)
        if white_noise is not None:
            cov = cov + white_noise
        return cov

    
class ExactGPModel(gpytorch.models.ExactGP):
    def __init__(self, train_x, train_y, likelihood, var=None, latent=None, kernel=None, **kwargs):
        super(ExactGPModel, self).__init__(train_x, train_y, likelihood)
        self._data_dims = train_x.size(-1)
        self._set_latent_function(latent, **kwargs) 
        
        self.mean_module = ZeroMean()
        ard_num_dims = self.latent_func.out_dims if self.latent_func.out_dims is not None else train_x.size(-1)
        
        if kernel is None or kernel == 'rbf':
            self.rbf_covar_module = RBFKernel(ard_num_dims=ard_num_dims)
        elif kernel == 'matern':
            self.rbf_covar_module = MaternKernel(nu=1.5, ard_num_dims=ard_num_dims)
        else:
            raise NotImplementedError

        # set covariance module
        if var is not None:
            self.noise_covar_module = WhiteNoiseKernel(var)
            self.covar_module = self.rbf_covar_module + self.noise_covar_module
        else:
            self.covar_module = self.rbf_covar_module
        
    def _set_latent_function(self, latent, **kwargs):
        if latent is None or latent == 'identity':
            self.latent_func = IdentityLatentFunction()
        elif latent == 'linear':
            self.latent_func = LinearLatentFunction(**kwargs)
        elif latent == 'non_linear':
            self.latent_func = NonLinearLatentFunction(**kwargs)
        elif latent == 'field':
            self.latent_func = FieldLatentFunction(**kwargs)
        else:
            raise NotImplementedError

    def forward(self, inp):
        x = self.latent_func(inp)
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return GaussianRandomVariable(mean_x, covar_x)


class IdentityLatentFunction(nn.Module):
    def __init__(self):
        super(IdentityLatentFunction, self).__init__()
        # self.apply(weights_init)
        self.out_dims = None

    def forward(self, x):
        return x


class LinearLatentFunction(nn.Module):
    def __init__(self, input_dim, embed_dim):
        super(LinearLatentFunction, self).__init__()
        self.fc = nn.Linear(input_dim, embed_dim)
        self.out_dims = embed_dim
        # self.apply(weights_init)

    def forward(self, x):
        return self.fc(x)


class NonLinearLatentFunction(nn.Module):
    def __init__(self, input_dim, f1_dim, f2_dim):
        super(NonLinearLatentFunction, self).__init__()
        self.fc1 = nn.Linear(input_dim, f1_dim)
        self.fc2 = nn.Linear(f1_dim, f2_dim)
        self.out_dims = f2_dim
        # self.apply(weights_init)

    def forward(self, inp):
        x = F.relu(self.fc1(inp))
        x = self.fc2(x)
        return x


# class FieldLatentFunction(nn.Module):
#     def __init__(self, rr_dim, v_dim):
#         super(FieldLatentFunction, self).__init__()
#         self.rr_dim = rr_dim
#         self.v_dim = v_dim
#         f1_dim = 3
#         f2_dim = 3
#         # NOTE: it may not be a good idea to perform transformation of rr dimensions
#         self.rr_fc = nn.Linear(self.rr_dim, f1_dim)
#         self.v_fc = nn.Linear(self.v_dim, f1_dim)
#         self.fc = nn.Linear(2*f1_dim, f2_dim)
#         # self.apply(weights_init)
#
#     def forward(self, inp):
#         # TODO: perhaps too many parameters (might overfit)
#         inp1, inp2 = torch.split(inp, [self.rr_dim, self.v_dim], dim=-1)
#         x1 = F.relu(self.rr_fc(inp1))
#         x2 = F.relu(self.v_fc(inp2))
#         x = torch.cat([x1, x2], dim=-1)
#         x = self.fc(x)
#         return x


class FieldLatentFunction(nn.Module):
    def __init__(self, rr_dim, v_dim):
        super(FieldLatentFunction, self).__init__()
        self.rr_dim = rr_dim
        self.v_dim = v_dim
        self.fc = nn.Linear(self.v_dim, 1, bias=False)
        self.out_dims = self.rr_dim + 1

    def forward(self, inp):
        inp1, inp2 = torch.split(inp, [self.rr_dim, self.v_dim], dim=-1)
        x = self.fc(inp2)
        x = torch.cat([inp1, x], dim=-1)
        return x


class GpytorchGPR(object):
    def __init__(self, latent=None, lr=.01, kernel=None, max_iterations=200):
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
        self.kernel = kernel
        self.max_iter = max_iterations
        # args = {'lr': lr, 'max_iter': max_iterations}
        # print('GPR model arguments:')
        # pprint(args)

    @property
    def train_x(self):
        return self._train_x.cpu().numpy()

    @property
    def train_var(self):
        return self._train_var.cpu().numpy()

    def reset(self, x, y, var, **kwargs):
        self._train_x = to_torch(x)
        self._train_y = to_torch(y)
        self._train_y_mean = self._train_y.mean()
        self._norm_train_y = self._train_y - self._train_y_mean
        if var is not None:
            self._train_var = to_torch(var)

        self.likelihood = GaussianLikelihood()
        self.model = ExactGPModel(self._train_x, self._norm_train_y, self.likelihood, self._train_var, self.latent, self.kernel, **kwargs)
        self.optimizer = torch.optim.Adam([{'params': self.model.parameters()}, ], lr=self.lr)
        self.mll = gpytorch.mlls.ExactMarginalLogLikelihood(self.likelihood, self.model)
        # self.lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, mode='min',patience=50,verbose=True)
            
    def fit(self, x, y, var=None, **kwargs):
        #  TODO: do we need non-zero var (probably yes for cholesky decomposition)
        if var is None:
            var = np.full(len(y), 1e-5)
        self.reset(x, y, var, **kwargs)
        self.model.train()
        self.likelihood.train()
        
        # NOTE: with zero mean, loss seems to converge
        # it may be a good idea to normalize the output values (check this)
        for i in range(self.max_iter):
            self.optimizer.zero_grad()
            output = self.model(self._train_x)
            loss = -self.mll(output, self._norm_train_y) # /len(y)
            loss.backward()
            self.optimizer.step()
            # self.lr_scheduler.step(loss)
            # print(i, loss.item())
            if i == 0:
                initial_ll = -loss.item()
            elif i == self.max_iter - 1:
                final_ll = -loss.item()

        print('Initial LogLikelihood {:.3f} Final LogLikelihood {:.3f}'.format(initial_ll, final_ll))
        # precomputing quantities for predictions
        K = self.cov_mat(self._train_x, white_noise=self._train_var)
        self.L_ = cholesky(K, lower=True)

    def cov_mat(self, x1, x2=None, white_noise=None):
        x1_ = to_torch(x1)
        x2_ = to_torch(x2)
        white_noise = to_torch(white_noise)
        flag = x2_ is None or torch.equal(x1_, x2_)

        # NOTE: set model to eval mode
        self.model.eval()
        with torch.no_grad():
            x1_ = self.model.latent_func(x1_)
            if flag:
                cov = self.model.covar_module(x1_).evaluate().cpu().numpy()
                # add white noise only for symmetric case
                if white_noise is not None:
                    cov = cov + torch.diag(white_noise)
            else:
                x2_ = self.model.latent_func(x2_)
                cov = self.model.covar_module(x1_, x2_).evaluate().cpu().numpy()
        return cov

    def predict(self, x, return_cov=False, return_std=False):
        self.model.eval()
        self.likelihood.eval()
        x_ = to_torch(x)

        with gpytorch.fast_pred_var() and torch.no_grad():
            pred = self.likelihood(self.model(x_))
            pred_mean = pred.mean().cpu().numpy()
            pred_mean += self._train_y_mean

            K_trans = self.cov_mat(x_, self._train_x)
            if return_std:
                L_inv = solve_triangular(self.L_.T, np.eye(self.L_.shape[0]))
                K_inv = L_inv.dot(L_inv.T)

                y_var = np.ones(x.shape[0])
                y_var -= np.einsum('ij,ij->i', np.dot(K_trans, K_inv), K_trans)
                y_var_negative = y_var < 0
                if np.any(y_var_negative):
                    warnings.warn("Predicted variances smaller than 0. "
                                  "Setting those variances to 0.")
                    y_var[y_var_negative] = 0.0
                return pred_mean, np.sqrt(y_var)

            elif return_cov:
                v = cho_solve((self.L_, True), K_trans.T)
                y_cov = self.cov_mat(x) - K_trans.dot(v)
                return pred_mean, y_cov

            return pred_mean