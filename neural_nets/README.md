# Neural network models
This module implements building-blocks for larger neural network models in the
Keras-style. This module does _not_ implement a general autograd system in order
emphasize conceptual understanding over flexibility. 

1. **Activations**. Common activation nonlinearities. Includes: 
    - Rectified linear units (ReLU)
    - Leaky rectified linear units
      ([Maas, Hannun, & Ng, 2013](https://ai.stanford.edu/~amaas/papers/relu_hybrid_icml2013_final.pdf))
    - Hyperbolic tangent (tanh)
    - Logistic sigmoid
    - Affine
    - Softmax

2. **Losses**. Common loss functions. Includes:
    - Squared error
    - Categorical cross entropy 
    - VAE Bernoulli loss ([Kingma & Welling, 2014](https://arxiv.org/abs/1312.6114))

3. **Wrappers**. Layer wrappers. Includes:
    - Dropout ([Srivastava, et al., 2014](http://www.jmlr.org/papers/volume15/srivastava14a/srivastava14a.pdf))

4. **Layers**. Common layers / layer-wise operations that can be composed to
   create larger neural networks. Includes:
    - Fully-connected 
    - 1D and 2D convolution (with stride, padding (`same`, `valid`, `causal`, etc), and dilation) ([van den Oord et al., 2016](https://arxiv.org/pdf/1609.03499.pdf); [Yu & Kolton, 2016](https://arxiv.org/pdf/1511.07122.pdf))
    - 2D "deconvolution" (with stride and padding) ([Zeiler et al., 2010](https://www.matthewzeiler.com/mattzeiler/deconvolutionalnetworks.pdf))
    - Restricted Boltzmann machines (with CD-_n_ training) ([Smolensky, 1996](http://stanford.edu/~jlmcc/papers/PDP/Volume%201/Chap6_PDP86.pdf); [Carreira-Perpiñán & Hinton, 2005](http://www.cs.toronto.edu/~fritz/absps/cdmiguel.pdf))
    - Elementwise multiplication
    - Summation
    - Flattening
    - Max & average pooling
    - 1D and 2D batch normalization ([Ioffe & Szegedy, 2015](http://proceedings.mlr.press/v37/ioffe15.pdf))
    - Recurrent ([Elman, 1990](https://crl.ucsd.edu/~elman/Papers/fsit.pdf))
    - Long short-term memory (LSTM) ([Hochreiter & Schmidhuber, 1997](http://www.bioinf.jku.at/publications/older/2604.pdf))

5. **Optimizers**. Common modifications to stochastic gradient descent.
   Includes:
    - SGD with momentum ([Rummelhart, Hinton, & Williams, 1986](https://www.cs.princeton.edu/courses/archive/spring18/cos495/res/backprop_old.pdf))
    - AdaGrad ([Duchi, Hazan, & Singer, 2011](http://jmlr.org/papers/volume12/duchi11a/duchi11a.pdf))
    - RMSProp ([Tieleman & Hinton, 2012](http://www.cs.toronto.edu/~tijmen/csc321/slides/lecture_slides_lec6.pdf))
    - Adam ([Kingma & Ba, 2015](https://arxiv.org/pdf/1412.6980v8.pdf))

6. **Initializers**. Common weight initialization strategies.
    - Glorot/Xavier uniform and normal ([Glorot & Bengio, 2010](http://jmlr.org/proceedings/papers/v9/glorot10a/glorot10a.pdf))
    - He/Kaiming uniform and normal ([He et al., 2015](https://arxiv.org/pdf/1502.01852v1.pdf))
    - Standard normal
    - Truncated normal

7. **Modules**. Common multi-layer blocks that appear across many deep networks.
   Includes: 
    - Bidirectional LSTMs ([Schuster & Paliwal, 1997](https://pdfs.semanticscholar.org/4b80/89bc9b49f84de43acc2eb8900035f7d492b2.pdf))
    - "Identity" (i.e., `same`-convolution) residual blocks ([He et al., 2015](https://arxiv.org/pdf/1512.03385.pdf))
    - "Convolutional" (i.e., parametric) residual blocks ([He et al., 2015](https://arxiv.org/pdf/1512.03385.pdf))
    - WaveNet-style residual block with dilated causal convolutions ([van den Oord et al., 2016](https://arxiv.org/pdf/1609.03499.pdf))

8. **Models**. Well-known network architectures. Includes:
    - `vae.py`: Bernoulli variational autoencoder ([Kingma & Welling, 2014](https://arxiv.org/abs/1312.6114))
    
8. **Utils**. Common helper functions, primarily for dealing with CNNs.
   Includes:
    - `im2col` 
    - `col2im` 
    - `conv1D` 
    - `conv2D`
    - `dilate`
    - `deconv2D` 
    - `minibatch`
    - Various weight initialization utilities
    - Various padding and convolution arithmetic utilities
