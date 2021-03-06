from abc import ABC, abstractmethod

import numpy as np

from initializers import WeightInitializer, OptimizerInitializer, ActivationInitializer
from wrappers import init_wrappers

from utils import (
    pad1D,
    pad2D,
    conv1D,
    conv2D,
    im2col,
    col2im,
    dilate,
    deconv2D_naive,
    calc_pad_dims_2D,
)


class LayerBase(ABC):
    def __init__(self, optimizer=None):
        self.X = None
        self.trainable = True
        self.optimizer = OptimizerInitializer(optimizer)()

        self.gradients = {}
        self.parameters = {}
        self.derived_variables = {}

        super().__init__()

    @abstractmethod
    def _init_params(self, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def forward(self, z, **kwargs):
        raise NotImplementedError

    @abstractmethod
    def backward(self, out, **kwargs):
        raise NotImplementedError

    def freeze(self):
        self.trainable = False

    def unfreeze(self):
        self.trainable = True

    def flush_gradients(self):
        assert self.trainable, "Layer is frozen"
        self.X = []
        for k, v in self.derived_variables.items():
            self.derived_variables[k] = None

        for k, v in self.gradients.items():
            self.gradients[k] = np.zeros_like(v)

    def update(self):
        assert self.trainable, "Layer is frozen"
        for k, v in self.gradients.items():
            if k in self.parameters:
                self.parameters[k] = self.optimizer(self.parameters[k], v, k)
        self.flush_gradients()

    def set_params(self, summary_dict):
        layer, sd = self, summary_dict

        # collapse `parameters` and `hyperparameters` nested dicts into a single
        # merged dictionary
        flatten_keys = ["parameters", "hyperparameters"]
        for k in flatten_keys:
            if k in sd:
                entry = sd[k]
                sd.update(entry)
                del sd[k]

        for k, v in sd.items():
            if k in self.parameters:
                layer.parameters[k] = v
            if k in self.hyperparameters:
                if k == "act_fn":
                    layer.act_fn = ActivationInitializer(v)()
                if k == "optimizer":
                    layer.optimizer = OptimizerInitializer(sd[k])()
                if k not in ["wrappers", "optimizer"]:
                    setattr(layer, k, v)
                if k == "wrappers":
                    layer = init_wrappers(layer, sd[k])
        return layer

    def summary(self):
        return {
            "layer": self.hyperparameters["layer"],
            "parameters": self.parameters,
            "hyperparameters": self.hyperparameters,
        }


class RestrictedBoltzmannMachine(LayerBase):
    def __init__(self, n_out, K=1, init="glorot_uniform", optimizer=None):
        """
        A Restricted Boltzmann machine with Bernoulli visible and hidden units.

        Parameters
        ----------
        n_out : int
            The number of output dimensions/units.
        K : int (default: 1)
            The number of contrastive divergence steps to run before computing
            a single gradient update.
        init : str (default: 'glorot_uniform')
            The weight initialization strategy. Valid entries are
            {'glorot_normal', 'glorot_uniform', 'he_normal', 'he_uniform'}
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.K = K  # CD-K
        self.init = init
        self.n_in = None
        self.n_out = n_out
        self.act_fn_V = ActivationInitializer("Sigmoid")()
        self.act_fn_H = ActivationInitializer("Sigmoid")()
        self.is_initialized = False
        self.parameters = {"W": None, "b_in": None, "b_out": None}

        self._init_params()

    def _init_params(self):
        init_weights = WeightInitializer(str(self.act_fn_V), mode=self.init)

        b_in = np.zeros((1, self.n_in))
        b_out = np.zeros((1, self.n_out))
        W = init_weights((self.n_in, self.n_out))

        self.parameters = {"W": W, "b_in": b_in, "b_out": b_out}

        self.gradients = {
            "W": np.zeros_like(W),
            "b_in": np.zeros_like(b_in),
            "b_out": np.zeros_like(b_out),
        }

        self.derived_variables = {
            "V": None,
            "p_H": None,
            "p_V_prime": None,
            "p_H_prime": None,
            "positive_grad": None,
            "negative_grad": None,
        }
        self.is_initialized = True

    @property
    def hyperparameters(self):
        return {
            "layer": "RestrictedBoltzmannMachine",
            "K": self.K,
            "n_in": self.n_in,
            "n_out": self.n_out,
            "init": self.init,
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def CD_update(self, X):
        """
        Perform a single contrastive divergence-k training update using the
        visible inputs X as a starting point for the Gibbs sampler.

        Parameters
        ----------
        X : numpy array of shape (n_ex, n_in)
            Layer input, representing the `n_in`-dimensional features for a
            minibatch of `n_ex` examples. Each feature in X should ideally be
            binary-valued, although it is possible to also train on real-valued
            features ranging between (0, 1) (e.g., grayscale images).
        """
        self.forward(X)
        self.backward()

    def forward(self, V, K=None):
        """
        Hinton recommends: http://www.cs.toronto.edu/~hinton/absps/guideTR.pdf
        Visible units:
            Use real-valued probabilities for both the data and the
            reconstructions
        Hidden units:
            For CD1: when the hidden units are being driven by data, always use
            stochastic binary states. When they are being driven by
            reconstructions, always use probabilities without sampling.
            For CD-k: only the final update of the hidden units should use the
            probability
        Updates:
            When collecting the pairwise statistics for learning weights or the
            individual statistics for learning biases, use the probabilities,
            not the binary states.

        Parameters
        ----------
        V : numpy array of shape (n_ex, n_in)
            Visible input, representing the `n_in`-dimensional features for a
            minibatch of `n_ex` examples. Each feature in V should ideally be
            binary-valued, although it is possible to also train on real-valued
            features ranging between (0, 1) (e.g., grayscale images).
        K : int (default: None)
            The number of steps of contrastive divergence steps to run before
            computing the gradient update. If `None`, use self.K
        """
        if not self.is_initialized:
            self.n_in = V.shape[1]
            self._init_params()

        # override self.K if necessary
        K = self.K if K is None else K

        W = self.parameters["W"]
        b_in = self.parameters["b_in"]
        b_out = self.parameters["b_out"]

        # compute hidden unit probabilities
        Z_H = np.dot(V, W) + b_out
        p_H = self.act_fn_H.fn(Z_H)

        # sample hidden states (stochastic binary values)
        H = np.random.rand(*p_H.shape) <= p_H
        H = H.astype(float)

        # always use probabilities when computing gradients
        positive_grad = np.dot(V.T, p_H)

        self.derived_variables["V"] = V
        self.derived_variables["p_H"] = p_H
        self.derived_variables["positive_grad"] = positive_grad

        # perform CD-k
        # TODO: use persistent CD-k
        # https://www.cs.toronto.edu/~tijmen/pcd/pcd.pdf
        H_prime = H.copy()
        for k in range(K):
            # resample v' given h (H_prime is binary for all but final step)
            Z_V_prime = np.dot(H_prime, W.T) + b_in
            p_V_prime = self.act_fn_V.fn(Z_V_prime)

            # don't resample visual units - always use raw probabilities!
            V_prime = p_V_prime

            # compute p(h' | v')
            Z_H_prime = np.dot(V_prime, W) + b_out
            p_H_prime = self.act_fn_H.fn(Z_H_prime)

            # if this is the final iteration of CD, keep hidden state
            # probabilities (don't sample)
            H_prime = p_H_prime
            if k != self.K - 1:
                H_prime = np.random.rand(*p_H_prime.shape) <= p_H_prime
                H_prime = H_prime.astype(float)

        negative_grad = np.dot(p_V_prime.T, p_H_prime)

        self.derived_variables["p_V_prime"] = p_V_prime
        self.derived_variables["p_H_prime"] = p_H_prime
        self.derived_variables["negative_grad"] = negative_grad

    def backward(self, *args):
        V = self.derived_variables["V"]
        p_H = self.derived_variables["p_H"]
        p_V_prime = self.derived_variables["p_V_prime"]
        p_H_prime = self.derived_variables["p_H_prime"]
        positive_grad = self.derived_variables["positive_grad"]
        negative_grad = self.derived_variables["negative_grad"]

        self.gradients["b_in"] = V - p_V_prime
        self.gradients["b_out"] = p_H - p_H_prime
        self.gradients["W"] = positive_grad - negative_grad

    def reconstruct(self, X, n_steps=10, return_prob=False):
        """
        Reconstruct an input X by running the trained Gibbs sampler for
        `n_steps`-worth of CD-k.

        Parameters
        ----------
        X : numpy array of shape (n_ex, n_in)
            Layer input, representing the `n_in`-dimensional features for a
            minibatch of `n_ex` examples. Each feature in X should ideally be
            binary-valued, although it is possible to also train on real-valued
            features ranging between (0, 1) (e.g., grayscale images). If X has
            missing values, it may be sufficient to mark them with random
            entries and allow the reconstruction to impute them.
        n_steps : int (default: 10)
            The number of Gibbs sampling steps to perform when generating the
            reconstruction.
        return_prob : bool (default: False)
            Whether to return the real-valued feature probabilities for the
            reconstruction or the binary samples.

        Returns
        -------
        V : numpy array of shape (n_ex, in_ch)
            The reconstruction (or feature probabilities if `return_prob` is
            true) of the visual input X after running the Gibbs sampler for
            `n_steps`.
        """
        self.forward(X, K=n_steps)
        p_V_prime = self.derived_variables["p_V_prime"]

        # ignore the gradients produced during this reconstruction
        self.flush_gradients()

        # sample V_prime reconstruction if return_prob is False
        V = p_V_prime
        if not return_prob:
            V = (np.random.rand(*p_V_prime.shape) <= p_V_prime).astype(float)
        return V


class Add(LayerBase):
    def __init__(self, act_fn=None, optimizer=None):
        """
        An "addition" layer that returns the sum of its inputs, passed through
        an optional nonlinearity.

        Parameters
        ----------
        act_fn : str or `activations.ActivationBase` instance (default: None)
            The element-wise output nonlinearity used in computing the final
            output. If `None`, use the identity function act_fn(x) = x.
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)
        self.act_fn = ActivationInitializer(act_fn)()
        self._init_params()

    def _init_params(self):
        self.derived_variables = {"sum": None}

    @property
    def hyperparameters(self):
        return {
            "layer": "Sum",
            "act_fn": str(self.act_fn),
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def forward(self, X):
        """
        Compute the layer output on a single minibatch.

        Parameters
        ----------
        X : list of length `n_inputs`
            A list of tensors, all of the same shape.

        Returns
        -------
        Y : numpy array of shape (n_ex, *dim)
            The sum over the `n_ex` examples
        """
        self.X = X
        out = X[0].copy()
        for i in range(1, len(self.X)):
            out += X[i]
        self.derived_variables["sum"] = out
        return self.act_fn.fn(out)

    def backward(self, dLdY):
        """
        Backprop from layer outputs to inputs

        Parameters
        ----------
        dLdY : numpy array of shape (n_ex, *dim)
            The gradient of the loss wrt. the layer output Y

        Returns
        -------
        dX : list of length `n_inputs`
            The gradient of the loss wrt. each input in `X`
        """
        _sum = self.derived_variables["sum"]
        grads = [dLdY * self.act_fn.grad(_sum) for z in self.X]
        return grads


class Multiply(LayerBase):
    def __init__(self, act_fn=None, optimizer=None):
        """
        A multiplication layer that returns the *elementwise* product of its
        inputs, passed through an optional nonlinearity.

        Parameters
        ----------
        act_fn : str or `activations.ActivationBase` instance (default: None)
            The element-wise output nonlinearity used in computing the final
            output. If `None`, use the identity function f(x) = x.
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)
        self.act_fn = ActivationInitializer(act_fn)()
        self._init_params()

    def _init_params(self):
        self.derived_variables = {"product": None}

    @property
    def hyperparameters(self):
        return {
            "layer": "Multiply",
            "act_fn": str(self.act_fn),
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def forward(self, X):
        """
        Compute the layer output on a single minibatch.

        Parameters
        ----------
        X : list of length `n_inputs`
            A list of tensors, all of the same shape.

        Returns
        -------
        Y : numpy array of shape (n_ex, *dim)
            The product over the `n_ex` examples
        """
        self.X = X
        out = X[0].copy()
        for i in range(1, len(self.X)):
            out *= X[i]
        self.derived_variables["product"] = out
        return self.act_fn.fn(out)

    def backward(self, dLdY):
        """
        Backprop from layer outputs to inputs

        Parameters
        ----------
        dLdY : numpy array of shape (n_ex, *dim)
            The gradient of the loss wrt. the layer output Y

        Returns
        -------
        dX : list of length `n_inputs`
            The gradient of the loss wrt. each input in `X`
        """
        _prod = self.derived_variables["product"]
        grads = [dLdY * self.act_fn.grad(_prod)] * len(self.X)
        for i, x in enumerate(self.X):
            grads = [g * x if j != i else g for j, g in enumerate(grads)]
        return grads


class BatchNorm2D(LayerBase):
    def __init__(self, momentum=0.9, epsilon=1e-5, optimizer=None):
        """
        A batch normalization layer for two-dimensional inputs with an
        additional channel dimension. This is sometimes known as "Spatial Batch
        Normalization" in the literature.

        Equations [train]:
            Y = scaler * norm(X) + intercept
            norm(X) = (X - mean(X)) / sqrt(var(X) + epsilon)

        Equations [test]:
            Y = scaler * running_norm(X) + intercept
            running_norm(X) = (X - running_mean) / sqrt(running_var + epsilon)

        Parameters
        ----------
        momentum : float (default: 0.9)
            The momentum term for the running mean/running std calculations.
            The closer this is to 1, the less weight will be given to the
            mean/std of the current batch (i.e., higher smoothing)
        epsilon : float (default : 1e-5)
            A small smoothing constant to use during computation of norm(X) to
            avoid divide-by-zero errors.
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.in_ch = None
        self.out_ch = None
        self.epsilon = epsilon
        self.momentum = momentum
        self.parameters = {
            "scaler": None,
            "intercept": None,
            "running_var": None,
            "running_mean": None,
        }
        self.is_initialized = False

    def _init_params(self):
        scaler = np.random.rand(self.in_ch)
        intercept = np.zeros(self.in_ch)

        # init running mean and std at 0 and 1, respectively
        running_mean = np.zeros(self.in_ch)
        running_var = np.ones(self.in_ch)

        self.parameters = {
            "scaler": scaler,
            "intercept": intercept,
            "running_var": running_var,
            "running_mean": running_mean,
        }

        self.gradients = {
            "scaler": np.zeros_like(scaler),
            "intercept": np.zeros_like(intercept),
        }

        self.is_initialized = True

    @property
    def hyperparameters(self):
        return {
            "layer": "BatchNorm2D",
            "act_fn": None,
            "in_ch": self.in_ch,
            "out_ch": self.out_ch,
            "epsilon": self.epsilon,
            "momentum": self.momentum,
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def reset_running_stats(self):
        assert self.trainable, "Layer is frozen"
        self.parameters["running_mean"] = np.zeros(self.in_ch)
        self.parameters["running_var"] = np.ones(self.in_ch)

    def forward(self, X):
        """
        Compute the layer output on a single minibatch.

        Equations [train]:
            Y = scaler * norm(X) + intercept
            norm(X) = (X - mean(X)) / sqrt(var(X) + epsilon)

        Equations [test]:
            Y = scaler * running_norm(X) + intercept
            running_norm(X) = (X - running_mean) / sqrt(running_var + epsilon)

        Parameters
        ----------
        X : numpy array of shape (n_ex, in_rows, in_cols, in_ch)
            Input volume containing the `in_rows` x `in_cols`-dimensional
            features for a minibatch of `n_ex` examples.

        Returns
        -------
        Y : numpy array of shape (n_ex, in_rows, in_cols, in_ch)
            Layer output for each of the `n_ex` examples
        """
        if not self.is_initialized:
            self.in_ch = self.out_ch = X.shape[3]
            self._init_params()

        self.X = X
        ep = self.hyperparameters["epsilon"]
        mm = self.hyperparameters["momentum"]
        rm = self.parameters["running_mean"]
        rv = self.parameters["running_var"]

        scaler = self.parameters["scaler"]
        intercept = self.parameters["intercept"]

        # if the layer is frozen, use our running mean/std values rather
        # than the mean/std values for the new batch
        X_mean = self.parameters["running_mean"]
        X_var = self.parameters["running_var"]

        if self.trainable:
            X_mean, X_var = X.mean(axis=(0, 1, 2)), X.var(axis=(0, 1, 2))  # , ddof=1)
            self.parameters["running_mean"] = mm * rm + (1.0 - mm) * X_mean
            self.parameters["running_var"] = mm * rv + (1.0 - mm) * X_var

        N = (X - X_mean) / np.sqrt(X_var + ep)
        y = scaler * N + intercept
        return y

    def backward(self, dLdy):
        """
        Backprop from layer outputs to inputs

        Parameters
        ----------
        dLdY : numpy array of shape (n_ex, in_rows, in_cols, in_ch)
            The gradient of the loss wrt. the layer output Y

        Returns
        -------
        dX : numpy array of shape (n_ex, in_rows, in_cols, in_ch)
            The gradient of the loss wrt. the layer input X
        """
        assert self.trainable, "Layer is frozen"

        scaler = self.parameters["scaler"]
        ep = self.hyperparameters["epsilon"]

        # reshape to 2D, retaining channel dim
        X = np.reshape(self.X, (-1, self.X.shape[3]))
        dLdy = np.reshape(dLdy, (-1, dLdy.shape[3]))

        # appy 1D batchnorm to reshaped array
        n_ex, in_ch = X.shape
        X_mean, X_var = X.mean(axis=0), X.var(axis=0)  # , ddof=1)

        N = (X - X_mean) / np.sqrt(X_var + ep)
        dIntercept = dLdy.sum(axis=0)
        dScaler = np.sum(dLdy * N, axis=0)

        dN = dLdy * scaler
        dX = (n_ex * dN - dN.sum(axis=0) - N * (dN * N).sum(axis=0)) / (
            n_ex * np.sqrt(X_var + ep)
        )

        # reshape gradients back to proper dimensions
        dX = np.reshape(dX, self.X.shape)
        self.gradients = {"scaler": dScaler, "intercept": dIntercept}
        return dX


class BatchNorm1D(LayerBase):
    def __init__(self, momentum=0.9, epsilon=1e-5, optimizer=None):
        """
        A batch normalization layer for vector inputs.

        Equations [train]:
            Y = scaler * norm(X) + intercept
            norm(X) = (X - mean(X)) / sqrt(var(X) + epsilon)

        Equations [test]:
            Y = scaler * running_norm(X) + intercept
            running_norm(X) = (X - running_mean) / sqrt(running_var + epsilon)

        Parameters
        ----------
        momentum : float (default: 0.9)
            The momentum term for the running mean/running std calculations.
            The closer this is to 1, the less weight will be given to the
            mean/std of the current batch (i.e., higher smoothing)
        epsilon : float (default : 1e-5)
            A small smoothing constant to use during computation of norm(X) to
            avoid divide-by-zero errors.
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.n_in = None
        self.n_out = None
        self.epsilon = epsilon
        self.momentum = momentum
        self.parameters = {
            "scaler": None,
            "intercept": None,
            "running_var": None,
            "running_mean": None,
        }
        self.is_initialized = False

    def _init_params(self):
        scaler = np.random.rand(self.n_in)
        intercept = np.zeros(self.n_in)

        # init running mean and std at 0 and 1, respectively
        running_mean = np.zeros(self.n_in)
        running_var = np.ones(self.n_in)

        self.parameters = {
            "scaler": scaler,
            "intercept": intercept,
            "running_mean": running_mean,
            "running_var": running_var,
        }

        self.gradients = {
            "scaler": np.zeros_like(scaler),
            "intercept": np.zeros_like(intercept),
        }
        self.is_initialized = True

    @property
    def hyperparameters(self):
        return {
            "layer": "BatchNorm1D",
            "act_fn": None,
            "n_in": self.n_in,
            "n_out": self.n_out,
            "epsilon": self.epsilon,
            "momentum": self.momentum,
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def reset_running_stats(self):
        assert self.trainable, "Layer is frozen"
        self.parameters["running_mean"] = np.zeros(self.n_in)
        self.parameters["running_var"] = np.ones(self.n_in)

    def forward(self, X):
        """
        Compute the layer output on a single minibatch.

        Equations [train]:
            Y = scaler * norm(X) + intercept
            norm(X) = (X - mean(X)) / sqrt(var(X) + epsilon)

        Equations [test]:
            Y = scaler * running_norm(X) + intercept
            running_norm(X) = (X - running_mean) / sqrt(running_var + epsilon)

        Parameters
        ----------
        X : numpy array of shape (n_ex, n_in)
            Layer input, representing the `n_in`-dimensional features for a
            minibatch of `n_ex` examples

        Returns
        -------
        Y : numpy array of shape (n_ex, n_in)
            Layer output for each of the `n_ex` examples
        """
        if not self.is_initialized:
            self.n_in = self.n_out = X.shape[1]
            self._init_params()

        self.X = X
        ep = self.hyperparameters["epsilon"]
        mm = self.hyperparameters["momentum"]
        rm = self.parameters["running_mean"]
        rv = self.parameters["running_var"]

        scaler = self.parameters["scaler"]
        intercept = self.parameters["intercept"]

        # if the layer is frozen, use our running mean/std values rather
        # than the mean/std values for the new batch
        X_mean = self.parameters["running_mean"]
        X_var = self.parameters["running_var"]

        if self.trainable:
            X_mean, X_var = X.mean(axis=0), X.var(axis=0)  # , ddof=1)
            self.parameters["running_mean"] = mm * rm + (1.0 - mm) * X_mean
            self.parameters["running_var"] = mm * rv + (1.0 - mm) * X_var

        N = (X - X_mean) / np.sqrt(X_var + ep)
        y = scaler * N + intercept
        return y

    def backward(self, dLdy):
        """
        Backprop from layer outputs to inputs

        Parameters
        ----------
        dLdY : numpy array of shape (n_ex, n_in)
            The gradient of the loss wrt. the layer output Y

        Returns
        -------
        dX : numpy array of shape (n_ex, n_in)
            The gradient of the loss wrt. the layer input X
        """
        assert self.trainable, "Layer is frozen"

        scaler = self.parameters["scaler"]
        ep = self.hyperparameters["epsilon"]

        X = self.X
        n_ex, n_in = X.shape
        X_mean, X_var = X.mean(axis=0), X.var(axis=0)  # , ddof=1)

        N = (X - X_mean) / np.sqrt(X_var + ep)
        dIntercept = dLdy.sum(axis=0)
        dScaler = np.sum(dLdy * N, axis=0)

        dN = dLdy * scaler
        dX = (n_ex * dN - dN.sum(axis=0) - N * (dN * N).sum(axis=0)) / (
            n_ex * np.sqrt(X_var + ep)
        )

        self.gradients = {"scaler": dScaler, "intercept": dIntercept}
        return dX


class FullyConnected(LayerBase):
    def __init__(self, n_out, act_fn=None, init="glorot_uniform", optimizer=None):
        """
        A fully-connected (dense) layer.

        Equations:
            Y = act_fn( W . X + b )

        Parameters
        ----------
        n_out : int
            The dimensionality of the layer output
        act_fn : str or `activations.ActivationBase` instance (default: None)
            The element-wise output nonlinearity used in computing Y. If None,
            use the identity function act_fn(X) = X
        init : str (default: 'glorot_uniform')
            The weight initialization strategy. Valid entries are
            {'glorot_normal', 'glorot_uniform', 'he_normal', 'he_uniform'}
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.init = init
        self.n_in = None
        self.n_out = n_out
        self.act_fn = ActivationInitializer(act_fn)()
        self.parameters = {"W": None, "b": None}
        self.is_initialized = False

    def _init_params(self):
        init_weights = WeightInitializer(str(self.act_fn), mode=self.init)

        b = np.zeros((1, self.n_out))
        W = init_weights((self.n_in, self.n_out))

        self.parameters = {"W": W, "b": b}
        self.derived_variables = {"Z": None, "Y": None}
        self.gradients = {"W": np.zeros_like(W), "b": np.zeros_like(b)}
        self.is_initialized = True

    @property
    def hyperparameters(self):
        return {
            "layer": "FullyConnected",
            "init": self.init,
            "n_in": self.n_in,
            "n_out": self.n_out,
            "act_fn": str(self.act_fn),
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def forward(self, X):
        """
        Compute the layer output on a single minibatch.

        Equations:
            Y = act_fn( W . X + b )

        Parameters
        ----------
        X : numpy array of shape (n_ex, n_in)
            Layer input, representing the `n_in`-dimensional features for a
            minibatch of `n_ex` examples

        Returns
        -------
        Y : numpy array of shape (n_ex, n_out)
            Layer output for each of the `n_ex` examples
        """
        if not self.is_initialized:
            self.n_in = X.shape[1]
            self._init_params()

        # save input for gradient calc during backward pass
        self.X = X

        # Retrieve parameters
        W = self.parameters["W"]
        b = self.parameters["b"]

        # compute next activation state
        Z = np.dot(X, W) + b
        Y = self.act_fn.fn(Z)
        self.derived_variables = {"Z": Z, "Y": Y}
        return Y

    def backward(self, dLdY):
        """
        Backprop from layer outputs to inputs

        Parameters
        ----------
        dLdY : numpy array of shape (n_ex, n_out)
            The gradient of the loss wrt. the layer output Y

        Returns
        -------
        dLdX : numpy array of shape (n_ex, n_in)
            The gradient of the loss wrt. the layer input X
        """
        assert self.trainable, "Layer is frozen"
        assert self.X is not None

        X = self.X
        W = self.parameters["W"]
        Z = self.derived_variables["Z"]

        # compute gradients
        dZ = dLdY * self.act_fn.grad(Z)
        dW = np.dot(X.T, dZ)
        dB = dZ.sum(axis=0, keepdims=True)
        dX = np.dot(dZ, W.T)

        self.gradients = {"W": dW, "b": dB, "Z": dZ, "Y": dLdY}
        return dX


class RNNCell(LayerBase):
    def __init__(self, n_out, act_fn="Tanh", init="glorot_uniform", optimizer=None):
        """
        A single step of a vanilla (Elman) RNN.

        Equations:
            Z[t] = Wax . X[t] + bax + Waa . A[t-1] + baa
            A[t] = act_fn(Z[t])

        We refer to A[t] as the hidden state at timestep t

        Parameters
        ----------
        n_out : int
            The dimension of a single hidden state / output on a given timestep
        act_fn : str or `activations.ActivationBase` instance (default: None)
            The activation function for computing A[t]. If not specified, use
            Tanh by default.
        init : str (default: 'glorot_uniform')
            The weight initialization strategy. Valid entries are
            {'glorot_normal', 'glorot_uniform', 'he_normal', 'he_uniform'}
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.init = init
        self.n_in = None
        self.n_out = n_out
        self.n_timesteps = None
        self.act_fn = ActivationInitializer(act_fn)()
        self.parameters = {"Waa": None, "Wax": None, "ba": None, "bx": None}
        self.is_initialized = False

    def _init_params(self):
        self.X = []
        init_weights = WeightInitializer(str(self.act_fn), mode=self.init)

        Wax = init_weights((self.n_in, self.n_out))
        Waa = init_weights((self.n_out, self.n_out))
        ba = np.zeros((self.n_out, 1))
        bx = np.zeros((self.n_out, 1))

        self.parameters = {"Waa": Waa, "Wax": Wax, "ba": ba, "bx": bx}

        self.gradients = {
            "Waa": np.zeros_like(Waa),
            "Wax": np.zeros_like(Wax),
            "ba": np.zeros_like(ba),
            "bx": np.zeros_like(bx),
        }

        self.derived_variables = {
            "A": [],
            "Z": [],
            "n_timesteps": 0,
            "current_step": 0,
            "dLdA_accumulator": None,
        }

        self.is_initialized = True

    @property
    def hyperparameters(self):
        return {
            "layer": "RNNCell",
            "init": self.init,
            "n_in": self.n_in,
            "n_out": self.n_out,
            "act_fn": str(self.act_fn),
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def forward(self, Xt):
        """
        Compute the network output for a single timestep.

        Equations:
            Z[t] = Wax . X[t] + bax + Waa . A[t-1] + baa
            A[t] = tanh(Z[t])

        We refer to A[t] as the hidden state at timestep t.

        Parameters
        ----------
        Xt : numpy array of shape (n_ex, n_in)
            Input at timestep t consisting of `n_ex` examples each of
            dimensionality `n_in`

        Returns
        -------
        At: numpy array of shape (n_ex, n_out)
            The value of the hidden state at timestep t for each of the `n_ex`
            examples
        """
        if not self.is_initialized:
            self.n_in = Xt.shape[1]
            self._init_params()

        # increment timestep
        self.derived_variables["n_timesteps"] += 1
        self.derived_variables["current_step"] += 1

        # Retrieve parameters
        ba = self.parameters["ba"]
        bx = self.parameters["bx"]
        Wax = self.parameters["Wax"]
        Waa = self.parameters["Waa"]

        # initialize the hidden state to zero
        As = self.derived_variables["A"]
        if len(As) == 0:
            n_ex, n_in = Xt.shape
            A0 = np.zeros((n_ex, self.n_out))
            As.append(A0)

        # compute next hidden state
        Zt = np.dot(As[-1], Waa) + ba.T + np.dot(Xt, Wax) + bx.T
        At = self.act_fn.fn(Zt)

        # store intermediate variables
        self.X.append(Xt)
        self.derived_variables["Z"].append(Zt)
        self.derived_variables["A"].append(At)
        return At

    def backward(self, dLdAt):
        """
        Backprop for a single timestep.

        Equations:
            Z[t] = Wax . X[t] + bax + Waa . A[t-1] + baa
            A[t] = tanh(Z[t])

        We refer to A[t] as the hidden state at timestep t.

        Parameters
        ----------
        dLdAt : numpy array of shape (n_ex, n_out)
            The gradient of the loss wrt. the layer outputs (ie., hidden
            states) at timestep t

        Returns
        -------
        dLdXt : numpy array of shape (n_ex, n_in)
            The gradient of the loss wrt. the layer inputs at timestep t
        """
        assert self.trainable, "Layer is frozen"

        #  decrement current step
        self.derived_variables["current_step"] -= 1

        # extract context variables
        Zs = self.derived_variables["Z"]
        As = self.derived_variables["A"]
        t = self.derived_variables["current_step"]
        dA_acc = self.derived_variables["dLdA_accumulator"]

        # initialize accumulator
        if dA_acc is None:
            dA_acc = np.zeros_like(As[0])

        # get network weights for gradient calcs
        Wax = self.parameters["Wax"]
        Waa = self.parameters["Waa"]

        # compute gradient components at timestep t
        dA = dLdAt + dA_acc
        dZ = self.act_fn.grad(Zs[t]) * dA
        dXt = np.dot(dZ, Wax.T)

        # update parameter gradients with signal from current step
        self.gradients["Waa"] += np.dot(As[t].T, dZ)
        self.gradients["Wax"] += np.dot(self.X[t].T, dZ)
        self.gradients["ba"] += dZ.sum(axis=0, keepdims=True).T
        self.gradients["bx"] += dZ.sum(axis=0, keepdims=True).T

        # update accumulator variable for hidden state
        self.derived_variables["dLdA_accumulator"] = np.dot(dZ, Waa.T)
        return dXt

    def flush_gradients(self):
        assert self.trainable, "Layer is frozen"

        self.X = []
        for k, v in self.derived_variables.items():
            self.derived_variables[k] = []

        self.derived_variables["n_timesteps"] = 0
        self.derived_variables["current_step"] = 0

        # reset parameter gradients to 0
        for k, v in self.parameters.items():
            self.gradients[k] = np.zeros_like(v)


class LSTMCell(LayerBase):
    def __init__(
        self,
        n_out,
        act_fn="Tanh",
        gate_fn="Sigmoid",
        init="glorot_uniform",
        optimizer=None,
    ):
        """
        A single step of a long short-term memory (LSTM) RNN.

        Notation:
            Z[t]  is the input to each of the gates at timestep t
            A[t]  is the value of the hidden state at timestep t
            Cc[t] is the value of the *candidate* cell/memory state at timestep t
            C[t]  is the value of the *final* cell/memory state at timestep t
            Gf[t] is the output of the forget gate at timestep t
            Gu[t] is the output of the update gate at timestep t
            Go[t] is the output of the output gate at timestep t

        Equations:
            Z[t]  = stack([A[t-1], X[t]])
            Gf[t] = gate_fn(Wf . Z[t] + bf)
            Gu[t] = gate_fn(Wu . Z[t] + bu)
            Go[t] = gate_fn(Wo . Z[t] + bo)
            Cc[t] = act_fn(Wc . Z[t] + bc)
            C[t]  = Gf[t] * C[t-1] + Gu[t] * Cc[t]
            A[t]  = Go[t] * act_fn(C[t])

            where '.' indicates dot/matrix product, and '*' indicates
            elementwise multiplication

        We refer to A[t] as the hidden state at timestep t and C[t] as the
        memory / cell state

        Parameters
        ----------
        n_out : int
            The dimension of a single hidden state / output on a given timestep
        act_fn : str or `activations.ActivationBase` instance (default: 'Tanh')
            The activation function for computing A[t].
        gate_fn : str or `activations.Activation` instance (default: 'Sigmoid')
            The gate function for computing the update, forget, and output
            gates.
        init : str (default: 'glorot_uniform')
            The weight initialization strategy. Valid entries are
            {'glorot_normal', 'glorot_uniform', 'he_normal', 'he_uniform'}
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.init = init
        self.n_in = None
        self.n_out = n_out
        self.n_timesteps = None
        self.act_fn = ActivationInitializer(act_fn)()
        self.gate_fn = ActivationInitializer(gate_fn)()
        self.parameters = {
            "Wf": None,
            "Wu": None,
            "Wc": None,
            "Wo": None,
            "bf": None,
            "bu": None,
            "bc": None,
            "bo": None,
        }
        self.is_initialized = False

    def _init_params(self):
        self.X = []
        init_weights_gate = WeightInitializer(str(self.gate_fn), mode=self.init)
        init_weights_act = WeightInitializer(str(self.act_fn), mode=self.init)

        Wf = init_weights_gate((self.n_in + self.n_out, self.n_out))
        Wu = init_weights_gate((self.n_in + self.n_out, self.n_out))
        Wc = init_weights_act((self.n_in + self.n_out, self.n_out))
        Wo = init_weights_gate((self.n_in + self.n_out, self.n_out))

        bf = np.zeros((1, self.n_out))
        bu = np.zeros((1, self.n_out))
        bc = np.zeros((1, self.n_out))
        bo = np.zeros((1, self.n_out))

        self.parameters = {
            "Wf": Wf,
            "Wu": Wu,
            "Wc": Wc,
            "Wo": Wo,
            "bf": bf,
            "bu": bu,
            "bc": bc,
            "bo": bo,
        }

        self.gradients = {
            "Wf": np.zeros_like(Wf),
            "Wu": np.zeros_like(Wu),
            "Wc": np.zeros_like(Wc),
            "Wo": np.zeros_like(Wo),
            "bf": np.zeros_like(bf),
            "bu": np.zeros_like(bu),
            "bc": np.zeros_like(bc),
            "bo": np.zeros_like(bo),
        }

        self.derived_variables = {
            "C": [],
            "A": [],
            "Gf": [],
            "Gu": [],
            "Go": [],
            "Gc": [],
            "Cc": [],
            "n_timesteps": 0,
            "current_step": 0,
            "dLdA_accumulator": None,
            "dLdC_accumulator": None,
        }

        self.is_initialized = True

    def _get_params(self):
        Wf = self.parameters["Wf"]
        Wu = self.parameters["Wu"]
        Wc = self.parameters["Wc"]
        Wo = self.parameters["Wo"]
        bf = self.parameters["bf"]
        bu = self.parameters["bu"]
        bc = self.parameters["bc"]
        bo = self.parameters["bo"]
        return Wf, Wu, Wc, Wo, bf, bu, bc, bo

    @property
    def hyperparameters(self):
        return {
            "layer": "LSTMCell",
            "init": self.init,
            "n_in": self.n_in,
            "n_out": self.n_out,
            "act_fn": str(self.act_fn),
            "gate_fn": str(self.gate_fn),
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def forward(self, Xt):
        """
        Compute the layer output for a single timestep.

        Notation:
            Z[t]  is the input to each of the gates at timestep t
            A[t]  is the value of the hidden state at timestep t
            Cc[t] is the value of the *candidate* cell/memory state at timestep t
            C[t]  is the value of the *final* cell/memory state at timestep t
            Gf[t] is the output of the forget gate at timestep t
            Gu[t] is the output of the update gate at timestep t
            Go[t] is the output of the output gate at timestep t

        Equations:
            Z[t]  = stack([A[t-1], X[t]])
            Gf[t] = gate_fn(Wf . Z[t] + bf)
            Gu[t] = gate_fn(Wu . Z[t] + bu)
            Go[t] = gate_fn(Wo . Z[t] + bo)
            Cc[t] = act_fn(Wc . Z[t] + bc)
            C[t]  = Gf[t] * C[t-1] + Gu[t] * Cc[t]
            A[t]  = Go[t] * act_fn(C[t])

            where '.' indicates dot/matrix product, and '*' indicates
            elementwise multiplication

        Parameters
        ----------
        Xt : numpy array of shape (n_ex, n_in)
            Input at timestep t consisting of `n_ex` examples each of
            dimensionality `n_in`

        Returns
        -------
        At: numpy array of shape (n_ex, n_out)
            The value of the hidden state at timestep t for each of the `n_ex`
            examples
        Ct: numpy array of shape (n_ex, n_out)
            The value of the cell/memory state at timestep t for each of the
            `n_ex` examples
        """
        if not self.is_initialized:
            self.n_in = Xt.shape[1]
            self._init_params()

        Wf, Wu, Wc, Wo, bf, bu, bc, bo = self._get_params()

        self.derived_variables["n_timesteps"] += 1
        self.derived_variables["current_step"] += 1

        if len(self.derived_variables["A"]) == 0:
            n_ex, n_in = Xt.shape
            init = np.zeros((n_ex, self.n_out))
            self.derived_variables["A"].append(init)
            self.derived_variables["C"].append(init)

        A_prev = self.derived_variables["A"][-1]
        C_prev = self.derived_variables["C"][-1]

        # concatenate A_prev and Xt to create Zt
        Zt = np.hstack([A_prev, Xt])

        Gft = self.gate_fn.fn(np.dot(Zt, Wf) + bf)
        Gut = self.gate_fn.fn(np.dot(Zt, Wu) + bu)
        Got = self.gate_fn.fn(np.dot(Zt, Wo) + bo)
        Cct = self.act_fn.fn(np.dot(Zt, Wc) + bc)
        Ct = Gft * C_prev + Gut * Cct
        At = Got * self.act_fn.fn(Ct)

        # bookkeeping
        self.X.append(Xt)
        self.derived_variables["A"].append(At)
        self.derived_variables["C"].append(Ct)
        self.derived_variables["Gf"].append(Gft)
        self.derived_variables["Gu"].append(Gut)
        self.derived_variables["Go"].append(Got)
        self.derived_variables["Cc"].append(Cct)
        return At, Ct

    def backward(self, dLdAt):
        """
        Backprop for a single timestep.

        Parameters
        ----------
        dLdAt : numpy array of shape (n_ex, n_out)
            The gradient of the loss wrt. the layer outputs (ie., hidden
            states) at timestep t

        Returns
        -------
        dLdXt : numpy array of shape (n_ex, n_in)
            The gradient of the loss wrt. the layer inputs at timestep t
        """
        assert self.trainable, "Layer is frozen"

        Wf, Wu, Wc, Wo, bf, bu, bc, bo = self._get_params()

        self.derived_variables["current_step"] -= 1
        t = self.derived_variables["current_step"]

        Got = self.derived_variables["Go"][t]
        Gft = self.derived_variables["Gf"][t]
        Gut = self.derived_variables["Gu"][t]
        Cct = self.derived_variables["Cc"][t]
        At = self.derived_variables["A"][t + 1]
        Ct = self.derived_variables["C"][t + 1]
        C_prev = self.derived_variables["C"][t]
        A_prev = self.derived_variables["A"][t]

        Xt = self.X[t]
        Zt = np.hstack([A_prev, Xt])

        dA_acc = self.derived_variables["dLdA_accumulator"]
        dC_acc = self.derived_variables["dLdC_accumulator"]

        # initialize accumulators
        if dA_acc is None:
            dA_acc = np.zeros_like(At)

        if dC_acc is None:
            dC_acc = np.zeros_like(Ct)

        # Gradient calculations
        # ---------------------

        dA = dLdAt + dA_acc
        dC = dC_acc + dA * Got * self.act_fn.grad(Ct)

        # compute the input to the gate functions at timestep t
        _Go = np.dot(Zt, Wo) + bo
        _Gf = np.dot(Zt, Wf) + bo
        _Gu = np.dot(Zt, Wu) + bo
        _Gc = np.dot(Zt, Wc) + bc

        # compute gradients wrt the *input* to each gate
        dGot = dA * self.act_fn.fn(Ct) * self.gate_fn.grad(_Go)
        dCct = dC * Gut * self.act_fn.grad(_Gc)
        dGut = dC * Cct * self.gate_fn.grad(_Gu)
        dGft = dC * C_prev * self.gate_fn.grad(_Gf)

        dZ = (
            np.dot(dGft, Wf.T)
            + np.dot(dGut, Wu.T)
            + np.dot(dCct, Wc.T)
            + np.dot(dGot, Wo.T)
        )

        dXt = dZ[:, self.n_out :]

        self.gradients["Wc"] += np.dot(Zt.T, dCct)
        self.gradients["Wu"] += np.dot(Zt.T, dGut)
        self.gradients["Wf"] += np.dot(Zt.T, dGft)
        self.gradients["Wo"] += np.dot(Zt.T, dGot)
        self.gradients["bo"] += dGot.sum(axis=0, keepdims=True)
        self.gradients["bu"] += dGut.sum(axis=0, keepdims=True)
        self.gradients["bf"] += dGft.sum(axis=0, keepdims=True)
        self.gradients["bc"] += dCct.sum(axis=0, keepdims=True)

        self.derived_variables["dLdA_accumulator"] = dZ[:, : self.n_out]
        self.derived_variables["dLdC_accumulator"] = Gft * dC
        return dXt

    def flush_gradients(self):
        assert self.trainable, "Layer is frozen"

        self.X = []
        for k, v in self.derived_variables.items():
            self.derived_variables[k] = []

        self.derived_variables["n_timesteps"] = 0
        self.derived_variables["current_step"] = 0

        # reset parameter gradients to 0
        for k, v in self.parameters.items():
            self.gradients[k] = np.zeros_like(v)


class Pool2D(LayerBase):
    def __init__(self, kernel_shape, stride=1, pad=0, mode="max", optimizer=None):
        """
        A single two-dimensional pooling layer.

        Parameters
        ----------
        kernel_shape : 2-tuple
            The dimension of a single 2D filter/kernel in the current layer
        stride : int (default: 1)
            The stride/hop of the convolution kernels as they move over the
            input volume
        pad : int, tuple, or 'same' (default: 0)
            The number of rows/columns of 0's to pad the input.
        mode : str (default: 'max')
            The pooling function to apply. Valid entries are {"max",
            "average"}.
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.pad = pad
        self.mode = mode
        self.in_ch = None
        self.out_ch = None
        self.stride = stride
        self.kernel_shape = kernel_shape
        self.is_initialized = False

    def _init_params(self):
        self.derived_variables = {"out_rows": None, "out_cols": None, "masks": []}
        self.is_initialized = True

    @property
    def hyperparameters(self):
        return {
            "layer": "Pool2D",
            "act_fn": None,
            "pad": self.pad,
            "mode": self.mode,
            "in_ch": self.in_ch,
            "out_ch": self.out_ch,
            "stride": self.stride,
            "kernel_shape": self.kernel_shape,
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def forward(self, X):
        """
        Backprop from layer outputs to inputs

        Parameters
        ----------
        dLdY : numpy array of shape (n_ex, in_rows, in_cols, in_ch)
            The gradient of the loss wrt. the layer output Y

        Returns
        -------
        dX : numpy array of shape (n_ex, in_rows, in_cols, in_ch)
            The gradient of the loss wrt. the layer input X
        """
        if not self.is_initialized:
            self.in_ch = self.out_ch = X.shape[3]
            self._init_params()

        self.X = X
        n_ex, in_rows, in_cols, nc_in = X.shape
        (fr, fc), s, p = self.kernel_shape, self.stride, self.pad
        X_pad, (pr1, pr2, pc1, pc2) = pad2D(X, p, self.kernel_shape, s)

        out_rows = np.floor(1 + (in_rows + pr1 + pr2 - fr) / s).astype(int)
        out_cols = np.floor(1 + (in_cols + pc1 + pc2 - fc) / s).astype(int)

        self.derived_variables["out_rows"] = out_rows
        self.derived_variables["out_cols"] = out_cols

        if self.mode == "max":
            pool_fn = np.max
        elif self.mode == "average":
            pool_fn = np.mean

        Y = np.zeros((n_ex, out_rows, out_cols, self.out_ch))
        for m in range(n_ex):
            for i in range(out_rows):
                for j in range(out_cols):
                    for c in range(self.out_ch):
                        # calculate window boundaries, incorporating stride
                        i0, i1 = i * s, (i * s) + fr
                        j0, j1 = j * s, (j * s) + fc

                        xi = X_pad[m, i0:i1, j0:j1, c]
                        Y[m, i, j, c] = pool_fn(xi)
        return Y

    def backward(self, dLdY):
        assert self.trainable, "Layer is frozen"

        X = self.X
        n_ex, in_rows, in_cols, nc_in = X.shape
        (fr, fc), s, p = self.kernel_shape, self.stride, self.pad
        X_pad, (pr1, pr2, pc1, pc2) = pad2D(X, p, self.kernel_shape, s)

        out_rows = self.derived_variables["out_rows"]
        out_cols = self.derived_variables["out_cols"]

        dX = np.zeros_like(X_pad)
        for m in range(n_ex):
            for i in range(out_rows):
                for j in range(out_cols):
                    for c in range(self.out_ch):
                        # calculate window boundaries, incorporating stride
                        i0, i1 = i * s, (i * s) + fr
                        j0, j1 = j * s, (j * s) + fc

                        dy = dLdY[m, i, j, c]
                        if self.mode == "max":
                            xi = self.X[m, i0:i1, j0:j1, c]

                            # enforce that the mask can only consist of a
                            # single `True` entry, even if multiple entries in
                            # xi are equal to max(xi)
                            mask = np.zeros_like(xi).astype(bool)
                            x, y = np.argwhere(xi == np.max(xi))[0]
                            mask[x, y] = True

                            dX[m, i0:i1, j0:j1, c] += mask * dy
                        elif self.mode == "average":
                            dX[m, i0:i1, j0:j1, c] += (
                                np.ones((fr, fc)) * dy / np.prod((fr, fc))
                            )

        pr2 = None if pr2 == 0 else -pr2
        pc2 = None if pc2 == 0 else -pc2
        dX = dX[:, pr1:pr2, pc1:pc2, :]
        return dX


class Flatten(LayerBase):
    def __init__(self, keep_dim="first", optimizer=None):
        """
        Flatten a multidimensional input into a 2D matrix.

        Parameters
        ----------
        keep_dim : str, int (default : 'first')
            The dimension of the original input to retain. Typically used for
            retaining the minibatch dimension. Valid entries are {'first',
            'last', -1} If -1, flatten all dimensions.
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.keep_dim = keep_dim
        self._init_params()

    def _init_params(self):
        self.X = None

        self.gradients = {}
        self.parameters = {}
        self.derived_variables = {}

    @property
    def hyperparameters(self):
        return {
            "layer": "Flatten",
            "keep_dim": self.keep_dim,
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def forward(self, X):
        self.in_dims = X.shape
        if self.keep_dim == -1:
            return X.flatten().reshape(1, -1)
        rs = (X.shape[0], -1) if self.keep_dim == "first" else (-1, X.shape[-1])
        return X.reshape(*rs)

    def backward(self, dLdy):
        return dLdy.reshape(*self.in_dims)


class Conv1D(LayerBase):
    def __init__(
        self,
        out_ch,
        kernel_width,
        pad=0,
        stride=1,
        dilation=0,
        act_fn=None,
        init="glorot_uniform",
        optimizer=None,
    ):
        """
        Apply a one-dimensional convolution kernel over an input volume.

        Equations:
            out = act_fn(pad(X) * W + b)
            out_dim = floor(1 + (n_rows_in + pad_left + pad_right - kernel_width) / stride)

            where '*' denotes the cross-correlation operation with stride `s` and dilation `d`

        Parameters
        ----------
        out_ch : int
            The number of filters/kernels to compute in the current layer
        kernel_width : int
            The width of a single 1D filter/kernel in the current layer
        act_fn : str or `activations.ActivationBase` instance (default: None)
            The activation function for computing Y[t]. If `None`, use the
            identity function f(x) = x by default
        pad : int, tuple, or {'same', 'causal'} (default: 0)
            The number of rows/columns to zero-pad the input with. If 'same',
            calculate padding to ensure the output length matches in the input
            length. If 'causal' compute padding such that the output both has
            the same length as the input AND output[t] does not depend on
            input[t + 1:].
        stride : int (default: 1)
            The stride/hop of the convolution kernels as they move over the
            input volume
        dilation : int (default: 0)
            Number of pixels inserted between kernel elements. Effective kernel
            shape after dilation is:
                [kernel_rows * (d + 1) - d, kernel_cols * (d + 1) - d]
        init : str (default: 'glorot_uniform')
            The weight initialization strategy. Valid entries are
            {'glorot_normal', 'glorot_uniform', 'he_normal', 'he_uniform'}
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.pad = pad
        self.init = init
        self.in_ch = None
        self.out_ch = out_ch
        self.stride = stride
        self.dilation = dilation
        self.kernel_width = kernel_width
        self.act_fn = ActivationInitializer(act_fn)()
        self.parameters = {"W": None, "b": None}
        self.is_initialized = False

    def _init_params(self):
        init_weights = WeightInitializer(str(self.act_fn), mode=self.init)

        W = init_weights((self.kernel_width, self.in_ch, self.out_ch))
        b = np.zeros((1, 1, self.out_ch))

        self.parameters = {"W": W, "b": b}

        self.gradients = {"W": np.zeros_like(W), "b": np.zeros_like(b)}
        self.derived_variables = {"Z": None, "out_rows": None, "out_cols": None}
        self.is_initialized = True

    @property
    def hyperparameters(self):
        return {
            "layer": "Conv1D",
            "pad": self.pad,
            "init": self.init,
            "in_ch": self.in_ch,
            "out_ch": self.out_ch,
            "stride": self.stride,
            "dilation": self.dilation,
            "act_fn": str(self.act_fn),
            "kernel_width": self.kernel_width,
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def forward(self, X):
        """
        Compute the layer output given input volume `X`.

        Parameters
        ----------
        X : numpy array of shape (n_ex, l_in, in_ch)
            The input volume consisting of `n_ex` examples, each of length
            `l_in` and with `in_ch` input channels

        Returns
        -------
        Y : numpy array of shape (n_ex, l_out, out_ch)
            The layer output
        """
        if not self.is_initialized:
            self.in_ch = X.shape[2]
            self._init_params()

        self.X = X
        W = self.parameters["W"]
        b = self.parameters["b"]

        n_ex, l_in, in_ch = X.shape
        s, p, d = self.stride, self.pad, self.dilation

        # pad the input and perform the forward convolution
        Z = conv1D(X, W, s, p, d) + b
        Y = self.act_fn.fn(Z)

        self.derived_variables["out_rows"] = Z.shape[1]
        self.derived_variables["out_cols"] = Z.shape[2]
        self.derived_variables["Z"] = Z

        return Y

    def backward(self, dLdY):
        """
        Compute the gradient of the loss with respect to the layer parameters.
        Relies on `im2col` and `col2im` to vectorize the gradient calculation.
        See the private method `_backward_naive` for a more straightforward
        implementation.

        Parameters
        ----------
        dLdY : numpy array of shape (n_ex, l_out, out_ch)
            The gradient of the loss with respect to the layer output.

        Returns
        -------
        dX : numpy array of shape (n_ex, l_in, in_ch)
            The gradient of the loss with respect to the layer input volume
        """
        X = self.X
        W = self.parameters["W"]
        Z = self.derived_variables["Z"]

        # add a row dimension to X, W, and dZ to permit us to use im2col/col2im
        X2D = np.expand_dims(X, axis=1)
        W2D = np.expand_dims(W, axis=0)
        dLdZ = np.expand_dims(dLdY * self.act_fn.grad(Z), axis=1)

        d = self.dilation
        fr, fc, in_ch, out_ch = W2D.shape
        n_ex, l_out, out_ch = dLdY.shape
        fr, fc, s = 1, self.kernel_width, self.stride

        # use pad1D here in order to correctly handle self.pad = 'causal',
        # which isn't defined for pad2D
        _, p = pad1D(X, self.pad, self.kernel_width, s, d)
        p2D = (0, 0, p[0], p[1])

        # columnize W, X, and dLdY
        dLdZ_col = dLdZ.transpose(3, 1, 2, 0).reshape(out_ch, -1)
        W_col = W2D.transpose(3, 2, 0, 1).reshape(out_ch, -1).T
        X_col, _ = im2col(X2D, W2D.shape, p2D, s, d)

        # compute gradients via matrix multiplication and reshape
        dB = dLdZ_col.sum(axis=1).reshape(1, 1, -1)
        dW = dLdZ_col.dot(X_col.T).reshape(out_ch, in_ch, fr, fc).transpose(2, 3, 1, 0)

        # reshape columnized dX back into the same format as the input volume
        dX_col = np.dot(W_col, dLdZ_col)
        dX = col2im(dX_col, X2D.shape, W2D.shape, p2D, s, d).transpose(0, 2, 3, 1)

        self.gradients["W"] = np.squeeze(dW, axis=0)
        self.gradients["b"] = dB
        return np.squeeze(dX, axis=1)

    def _backward_naive(self, dLdY):
        """
        A slower (ie., non-vectorized) but more straightforward implementation
        of the gradient computations for a 2D conv layer.

        Parameters
        ----------
        dLdY : numpy array of shape (n_ex, l_out, out_ch)
            The gradient of the loss with respect to the layer output.

        Returns
        -------
        dX : numpy array of shape (n_ex, l_in, in_ch)
            The gradient of the loss with respect to the layer input volume
        """
        W = self.parameters["W"]
        b = self.parameters["b"]
        Z = self.derived_variables["Z"]

        X, d = self.X, self.dilation
        n_ex, l_out, out_ch = dLdY.shape
        fw, s, p = self.kernel_width, self.stride, self.pad
        X_pad, (pr1, pr2) = pad1D(X, p, self.kernel_width, s, d)

        dZ = dLdY * self.act_fn.grad(Z)

        dX = np.zeros_like(X_pad)
        dW, dB = np.zeros_like(W), np.zeros_like(b)
        for m in range(n_ex):
            for i in range(l_out):
                for c in range(out_ch):
                    # compute window boundaries w. stride and dilation
                    i0, i1 = i * s, (i * s) + fw * (d + 1) - d

                    wc = W[:, :, c]
                    kernel = dZ[m, i, c]
                    window = X_pad[m, i0 : i1 : (d + 1), :]

                    dB[:, :, c] += kernel
                    dW[:, :, c] += window * kernel
                    dX[m, i0 : i1 : (d + 1), :] += wc * kernel

        self.gradients["W"] = dW
        self.gradients["b"] = dB

        pr2 = None if pr2 == 0 else -pr2
        return dX[:, pr1:pr2, :]


class Conv2D(LayerBase):
    def __init__(
        self,
        out_ch,
        kernel_shape,
        pad=0,
        stride=1,
        dilation=0,
        act_fn=None,
        init="glorot_uniform",
        optimizer=None,
    ):
        """
        Apply a two-dimensional convolution kernel over an input volume.

        Equations:
            out = act_fn(pad(X) * W + b)
            n_rows_out = floor(1 + (n_rows_in + pad_left + pad_right - filter_rows) / stride)
            n_cols_out = floor(1 + (n_cols_in + pad_top + pad_bottom - filter_cols) / stride)

            where '*' denotes the cross-correlation operation with stride `s` and dilation `d`

        Parameters
        ----------
        out_ch : int
            The number of filters/kernels to compute in the current layer
        kernel_shape : 2-tuple
            The dimension of a single 2D filter/kernel in the current layer
        act_fn : str or `activations.ActivationBase` instance (default: None)
            The activation function for computing Y[t]. If `None`, use the
            identity function f(X) = X by default
        pad : int, tuple, or 'same' (default: 0)
            The number of rows/columns to zero-pad the input with
        stride : int (default: 1)
            The stride/hop of the convolution kernels as they move over the
            input volume
        dilation : int (default: 0)
            Number of pixels inserted between kernel elements. Effective kernel
            shape after dilation is:
                [kernel_rows * (d + 1) - d, kernel_cols * (d + 1) - d]
        init : str (default: 'glorot_uniform')
            The weight initialization strategy. Valid entries are
            {'glorot_normal', 'glorot_uniform', 'he_normal', 'he_uniform'}
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.pad = pad
        self.init = init
        self.in_ch = None
        self.out_ch = out_ch
        self.stride = stride
        self.dilation = dilation
        self.kernel_shape = kernel_shape
        self.act_fn = ActivationInitializer(act_fn)()
        self.parameters = {"W": None, "b": None}
        self.is_initialized = False

    def _init_params(self):
        init_weights = WeightInitializer(str(self.act_fn), mode=self.init)

        fr, fc = self.kernel_shape
        W = init_weights((fr, fc, self.in_ch, self.out_ch))
        b = np.zeros((1, 1, 1, self.out_ch))

        self.parameters = {"W": W, "b": b}
        self.gradients = {"W": np.zeros_like(W), "b": np.zeros_like(b)}
        self.derived_variables = {"Z": None, "out_rows": None, "out_cols": None}
        self.is_initialized = True

    @property
    def hyperparameters(self):
        return {
            "layer": "Conv2D",
            "pad": self.pad,
            "init": self.init,
            "in_ch": self.in_ch,
            "out_ch": self.out_ch,
            "stride": self.stride,
            "dilation": self.dilation,
            "act_fn": str(self.act_fn),
            "kernel_shape": self.kernel_shape,
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def forward(self, X):
        """
        Compute the layer output given input volume `X`.

        Parameters
        ----------
        X : numpy array of shape (n_ex, in_rows, in_cols, in_ch)
            The input volume consisting of `n_ex` examples, each with dimension
            (in_rows x in_cols x in_ch)

        Returns
        -------
        Y : numpy array of shape (n_ex, out_rows, out_cols, out_ch)
            The layer output
        """
        if not self.is_initialized:
            self.in_ch = X.shape[3]
            self._init_params()

        self.X = X

        W = self.parameters["W"]
        b = self.parameters["b"]

        n_ex, in_rows, in_cols, in_ch = X.shape
        s, p, d = self.stride, self.pad, self.dilation

        # pad the input and perform the forward convolution
        Z = conv2D(X, W, s, p, d) + b
        Y = self.act_fn.fn(Z)

        self.derived_variables["out_rows"] = Z.shape[1]
        self.derived_variables["out_cols"] = Z.shape[2]
        self.derived_variables["Z"] = Z

        return Y

    def backward(self, dLdY):
        """
        Compute the gradient of the loss with respect to the layer parameters.
        Relies on `im2col` and `col2im` to vectorize the gradient calculation.
        See the private method `_backward_naive` for a more straightforward
        implementation.

        Parameters
        ----------
        dLdY : numpy array of shape (n_ex, out_rows, out_cols, out_ch)
            The gradient of the loss with respect to the layer output.

        Returns
        -------
        dX : numpy array of shape (n_ex, in_rows, in_cols, in_ch)
            The gradient of the loss with respect to the layer input volume
        """
        X = self.X
        W = self.parameters["W"]
        Z = self.derived_variables["Z"]

        d = self.dilation
        fr, fc, in_ch, out_ch = W.shape
        n_ex, out_rows, out_cols, out_ch = dLdY.shape
        (fr, fc), s, p = self.kernel_shape, self.stride, self.pad

        # columnize W, X, and dLdY
        dLdZ = dLdY * self.act_fn.grad(Z)
        dLdZ_col = dLdZ.transpose(3, 1, 2, 0).reshape(out_ch, -1)
        W_col = W.transpose(3, 2, 0, 1).reshape(out_ch, -1).T
        X_col, p = im2col(X, W.shape, p, s, d)

        # compute gradients via matrix multiplication and reshape
        dB = dLdZ_col.sum(axis=1).reshape(1, 1, 1, -1)
        dW = dLdZ_col.dot(X_col.T).reshape(out_ch, in_ch, fr, fc).transpose(2, 3, 1, 0)

        # reshape columnized dX back into the same format as the input volume
        dX_col = np.dot(W_col, dLdZ_col)
        dX = col2im(dX_col, X.shape, W.shape, p, s, d).transpose(0, 2, 3, 1)

        self.gradients["W"] = dW
        self.gradients["b"] = dB
        return dX

    def _backward_naive(self, dLdY):
        """
        A slower (ie., non-vectorized) but more straightforward implementation
        of the gradient computations for a 2D conv layer.

        Parameters
        ----------
        dLdY : numpy array of shape (n_ex, out_rows, out_cols, out_ch)
            The gradient of the loss with respect to the layer output.

        Returns
        -------
        dX : numpy array of shape (n_ex, in_rows, in_cols, in_ch)
            The gradient of the loss with respect to the layer input volume
        """
        W = self.parameters["W"]
        b = self.parameters["b"]
        Z = self.derived_variables["Z"]

        X, d = self.X, self.dilation
        n_ex, out_rows, out_cols, out_ch = dLdY.shape
        (fr, fc), s, p = self.kernel_shape, self.stride, self.pad
        X_pad, (pr1, pr2, pc1, pc2) = pad2D(X, p, self.kernel_shape, s, d)

        dZ = dLdY * self.act_fn.grad(Z)

        dX = np.zeros_like(X_pad)
        dW, dB = np.zeros_like(W), np.zeros_like(b)
        for m in range(n_ex):
            for i in range(out_rows):
                for j in range(out_cols):
                    for c in range(out_ch):
                        # compute window boundaries w. stride and dilation
                        i0, i1 = i * s, (i * s) + fr * (d + 1) - d
                        j0, j1 = j * s, (j * s) + fc * (d + 1) - d

                        wc = W[:, :, :, c]
                        kernel = dZ[m, i, j, c]
                        window = X_pad[m, i0 : i1 : (d + 1), j0 : j1 : (d + 1), :]

                        dB[:, :, :, c] += kernel
                        dW[:, :, :, c] += window * kernel
                        dX[m, i0 : i1 : (d + 1), j0 : j1 : (d + 1), :] += wc * kernel

        self.gradients["W"] = dW
        self.gradients["b"] = dB

        pr2 = None if pr2 == 0 else -pr2
        pc2 = None if pc2 == 0 else -pc2
        dX = dX[:, pr1:pr2, pc1:pc2, :]
        return dX


class Deconv2D(LayerBase):
    def __init__(
        self,
        out_ch,
        kernel_shape,
        pad=0,
        stride=1,
        act_fn=None,
        optimizer=None,
        init="glorot_uniform",
    ):
        """
        Apply a two-dimensional "deconvolution" (more accurately, a transposed
        convolution / fractionally-strided convolution) to an input volume.

        Parameters
        ----------
        out_ch : int
            The number of filters/kernels to compute in the current layer
        kernel_shape : 2-tuple
            The dimension of a single 2D filter/kernel in the current layer
        act_fn : str or `activations.ActivationBase` instance (default: None)
            The activation function for computing Y[t]. If `None`, use Affine
            activations by default
        pad : int, tuple, or 'same' (default: 0)
            The number of rows/columns to zero-pad the input with
        stride : int (default: 1)
            The stride/hop of the convolution kernels as they move over the
            input volume
        init : str (default: 'glorot_uniform')
            The weight initialization strategy. Valid entries are
            {'glorot_normal', 'glorot_uniform', 'he_normal', 'he_uniform'}
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.pad = pad
        self.init = init
        self.in_ch = None
        self.stride = stride
        self.out_ch = out_ch
        self.kernel_shape = kernel_shape
        self.act_fn = ActivationInitializer(act_fn)()
        self.parameters = {"W": None, "b": None}
        self.is_initialized = False

    def _init_params(self):
        init_weights = WeightInitializer(str(self.act_fn), mode=self.init)

        fr, fc = self.kernel_shape
        W = init_weights((fr, fc, self.in_ch, self.out_ch))
        b = np.zeros((1, 1, 1, self.out_ch))

        self.parameters = {"W": W, "b": b}
        self.gradients = {"W": np.zeros_like(W), "b": np.zeros_like(b)}
        self.derived_variables = {"Z": None, "out_rows": None, "out_cols": None}
        self.is_initialized = True

    @property
    def hyperparameters(self):
        return {
            "layer": "Deconv2D",
            "pad": self.pad,
            "init": self.init,
            "in_ch": self.in_ch,
            "out_ch": self.out_ch,
            "stride": self.stride,
            "act_fn": str(self.act_fn),
            "kernel_shape": self.kernel_shape,
            "optimizer": {
                "cache": self.optimizer.cache,
                "hyperparameters": self.optimizer.hyperparameters,
            },
        }

    def forward(self, X):
        """
        Compute the layer output given input volume `X`.

        Parameters
        ----------
        X : numpy array of shape (n_ex, in_rows, in_cols, in_ch)
            The input volume consisting of `n_ex` examples, each with dimension
            (in_rows x in_cols x in_ch)

        Returns
        -------
        Y : numpy array of shape (n_ex, out_rows, out_cols, out_ch)
            The layer output
        """
        if not self.is_initialized:
            self.in_ch = X.shape[3]
            self._init_params()

        self.X = X
        W = self.parameters["W"]
        b = self.parameters["b"]

        s, p = self.stride, self.pad
        n_ex, in_rows, in_cols, in_ch = X.shape

        # pad the input and perform the forward deconvolution
        Z = deconv2D_naive(X, W, s, p, 0) + b
        Y = self.act_fn.fn(Z)

        self.derived_variables["out_rows"] = Z.shape[1]
        self.derived_variables["out_cols"] = Z.shape[2]
        self.derived_variables["Z"] = Z

        return Y

    def backward(self, dLdY):
        """
        Compute the gradient of the loss with respect to the layer parameters.
        Relies on `im2col` and `col2im` to vectorize the gradient calculations.

        Parameters
        ----------
        dLdY : numpy array of shape (n_ex, out_rows, out_cols, out_ch)
            The gradient of the loss with respect to the layer output.

        Returns
        -------
        dX : numpy array of shape (n_ex, in_rows, in_cols, in_ch)
            The gradient of the loss with respect to the layer input volume
        """
        X = self.X
        Z = self.derived_variables["Z"]
        W = np.rot90(self.parameters["W"], 2)

        s = self.stride
        if self.stride > 1:
            X = dilate(X, s - 1)
            s = 1

        fr, fc, in_ch, out_ch = W.shape
        (fr, fc), p = self.kernel_shape, self.pad
        n_ex, out_rows, out_cols, out_ch = dLdY.shape

        # pad X the first time
        X_pad, p = pad2D(X, p, W.shape[:2], s)
        n_ex, in_rows, in_cols, in_ch = X_pad.shape
        pr1, pr2, pc1, pc2 = p

        # compute padding for the deconvolution
        out_rows = s * (in_rows - 1) - pr1 - pr2 + fr
        out_cols = s * (in_cols - 1) - pc1 - pc2 + fc
        out_dim = (out_rows, out_cols)

        _p = calc_pad_dims_2D(X_pad.shape, out_dim, W.shape[:2], s, 0)
        X_pad, _ = pad2D(X_pad, _p, W.shape[:2], s)

        # columnize W, X, and dLdY
        dLdZ = dLdY * self.act_fn.grad(Z)
        dLdZ, _ = pad2D(dLdZ, p, W.shape[:2], s)

        dLdZ_col = dLdZ.transpose(3, 1, 2, 0).reshape(out_ch, -1)
        W_col = W.transpose(3, 2, 0, 1).reshape(out_ch, -1).T
        X_col, _ = im2col(X_pad, W.shape, 0, s, 0)

        # compute gradients via matrix multiplication and reshape
        dB = dLdZ_col.sum(axis=1).reshape(1, 1, 1, -1)
        dW = dLdZ_col.dot(X_col.T).reshape(out_ch, in_ch, fr, fc).transpose(2, 3, 1, 0)

        # reshape columnized dX back into the same format as the input volume
        dX_col = np.dot(W_col, dLdZ_col)

        total_pad = tuple(i + j for i, j in zip(p, _p))
        dX = col2im(dX_col, X.shape, W.shape, total_pad, s, 0).transpose(0, 2, 3, 1)

        # rotate gradient back
        self.gradients["W"] = np.rot90(dW, 2)
        self.gradients["b"] = dB
        return dX[:, :: self.stride, :: self.stride, :]


class RNN(LayerBase):
    def __init__(self, n_out, act_fn="Tanh", init="glorot_uniform", optimizer=None):
        """
        A single vanilla (Elman)-RNN layer.

        Parameters
        ----------
        n_out : int
            The dimension of a single hidden state / output on a given timestep
        act_fn : str or `activations.ActivationBase` instance (default: 'Tanh')
            The activation function for computing A[t].
        init : str (default: 'glorot_uniform')
            The weight initialization strategy. Valid entries are
            {'glorot_normal', 'glorot_uniform', 'he_normal', 'he_uniform'}
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.init = init
        self.n_in = None
        self.n_out = n_out
        self.n_timesteps = None
        self.act_fn = ActivationInitializer(act_fn)()
        self.is_initialized = False

    def _init_params(self):
        self.cell = RNNCell(
            n_in=self.n_in,
            n_out=self.n_out,
            act_fn=self.act_fn,
            init=self.init,
            optimizer=self.optimizer,
        )
        self.is_initialized = True

    @property
    def hyperparameters(self):
        return {
            "layer": "RNN",
            "init": self.init,
            "n_in": self.n_in,
            "n_out": self.n_out,
            "act_fn": str(self.act_fn),
            "optimizer": self.cell.hyperparameters["optimizer"],
        }

    def forward(self, X):
        if not self.is_initialized:
            self.n_in = X.shape[1]
            self._init_params()

        Y = []
        n_ex, n_in, n_t = X.shape
        for t in range(n_t):
            yt = self.cell.forward(X[:, :, t])
            Y.append(yt)
        return np.dstack(Y)

    def backward(self, dLdA):
        assert self.cell.trainable, "Layer is frozen"
        dLdX = []
        n_ex, n_out, n_t = dLdA.shape
        for t in reversed(range(n_t)):
            dLdXt = self.cell.backward(dLdA[:, :, t])
            dLdX.insert(0, dLdXt)
        dLdX = np.dstack(dLdX)
        return dLdX

    @property
    def derived_variables(self):
        return self.cell.derived_variables

    @property
    def gradients(self):
        return self.cell.gradients

    @property
    def parameters(self):
        return self.cell.parameters

    def set_params(self, summary_dict):
        self = super().set_params(summary_dict)
        return self.cell.set_parameters(summary_dict)

    def freeze(self):
        self.cell.freeze()

    def unfreeze(self):
        self.cell.unfreeze()

    def flush_gradients(self):
        self.cell.flush_gradients()

    def update(self):
        self.cell.update()
        self.flush_gradients()


class LSTM(LayerBase):
    def __init__(
        self,
        n_out,
        act_fn="Tanh",
        gate_fn="Sigmoid",
        init="glorot_uniform",
        optimizer=None,
    ):
        """
        A single long short-term memory (LSTM) RNN layer.

        Parameters
        ----------
        n_out : int
            The dimension of a single hidden state / output on a given timestep
        act_fn : str or `activations.ActivationBase` instance (default: 'Tanh')
            The activation function for computing A[t].
        gate_fn : str or `activations.Activation` instance (default: 'Sigmoid')
            The gate function for computing the update, forget, and output
            gates.
        init : str (default: 'glorot_uniform')
            The weight initialization strategy. Valid entries are
            {'glorot_normal', 'glorot_uniform', 'he_normal', 'he_uniform'}
        optimizer : str or `OptimizerBase` instance (default: None)
            The optimization strategy to use when performing gradient updates
            within the `update` method.  If `None`, use the `SGD` optimizer with
            default parameters.
        """
        super().__init__(optimizer)

        self.init = init
        self.n_in = None
        self.n_out = n_out
        self.n_timesteps = None
        self.act_fn = ActivationInitializer(act_fn)()
        self.gate_fn = ActivationInitializer(gate_fn)()
        self.is_initialized = False

    def _init_params(self):
        self.cell = LSTMCell(
            n_in=self.n_in,
            n_out=self.n_out,
            act_fn=self.act_fn,
            gate_fn=self.gate_fn,
            init=self.init,
        )
        self.is_initialized = True

    @property
    def hyperparameters(self):
        return {
            "layer": "LSTM",
            "init": self.init,
            "n_in": self.n_in,
            "n_out": self.n_out,
            "act_fn": str(self.act_fn),
            "gate_fn": str(self.gate_fn),
            "optimizer": self.cell.hyperparameters["optimizer"],
        }

    def forward(self, X):
        """
        Run a forward pass across all timesteps in the input.

        Parameters
        ----------
        X : numpy array of shape (n_ex, n_in, n_t)
            Input consisting of `n_ex` examples each of dimensionality `n_in`
            and extending for `n_t` timesteps

        Returns
        -------
        Y : numpy array of shape (n_ex, n_out, n_t)
            The value of the hidden state for each of the `n_ex` examples
            across each of the `n_t` timesteps
        """
        if not self.is_initialized:
            self.n_in = X.shape[1]
            self._init_params()

        Y = []
        n_ex, n_in, n_t = X.shape
        for t in range(n_t):
            yt, _ = self.cell.forward(X[:, :, t])
            Y.append(yt)
        return np.dstack(Y)

    def backward(self, dLdA):
        """
        Run a backward pass across all timesteps in the input.

        Parameters
        ----------
        dLdA : numpy array of shape (n_ex, n_out, n_t)
            The gradient of the loss with respect to the layer output for each
            of the `n_ex` examples across all `n_t` timesteps

        Returns
        -------
        dLdX : numpy array of shape (n_ex, n_in, n_t)
            The value of the hidden state for each of the `n_ex` examples
            across each of the `n_t` timesteps
        """
        assert self.cell.trainable, "Layer is frozen"
        dLdX = []
        n_ex, n_out, n_t = dLdA.shape
        for t in reversed(range(n_t)):
            dLdXt, _ = self.cell.backward(dLdA[:, :, t])
            dLdX.insert(0, dLdXt)
        dLdX = np.dstack(dLdX)
        return dLdX

    @property
    def derived_variables(self):
        return self.cell.derived_variables

    @property
    def gradients(self):
        return self.cell.gradients

    @property
    def parameters(self):
        return self.cell.parameters

    def freeze(self):
        self.cell.freeze()

    def unfreeze(self):
        self.cell.unfreeze()

    def set_params(self, summary_dict):
        self = super().set_params(summary_dict)
        return self.cell.set_parameters(summary_dict)

    def flush_gradients(self):
        self.cell.flush_gradients()

    def update(self):
        self.cell.update()
        self.flush_gradients()
