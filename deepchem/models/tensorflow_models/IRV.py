from __future__ import print_function
from __future__ import division
from __future__ import unicode_literals

import numpy as np
import tensorflow as tf

from deepchem.utils.save import log
from deepchem.models.tensorgraph.tensor_graph import TensorGraph
from deepchem.models.tensorgraph.layers import Layer, SigmoidCrossEntropy, \
    Sigmoid, Feature, Label, Weights, Concat, WeightedError
from deepchem.models.tensorgraph.layers import convert_to_layers
from deepchem.trans import undo_transforms

class IRVLayer(Layer):
  """ Core layer of IRV classifier, architecture described in:
       https://www.ncbi.nlm.nih.gov/pmc/articles/PMC2750043/
  """

  def __init__(self,
               n_tasks,
               K,
               **kwargs):
    """
    Parameters
    ----------
    n_tasks: int
      Number of tasks
    K: int
      Number of nearest neighbours used in classification
    """
    self.n_tasks = n_tasks
    self.K = K
    self.V, self.W, self.b, self.b2 = None, None, None, None
    super(IRVLayer, self).__init__(**kwargs)

  def build(self):
    self.V = tf.Variable(tf.constant([0.01, 1.]), name="vote", dtype=tf.float32)
    self.W = tf.Variable(tf.constant([1., 1.]), name="w", dtype=tf.float32)
    self.b = tf.Variable(tf.constant([0.01]), name="b", dtype=tf.float32)
    self.b2 = tf.Variable(tf.constant([0.01]), name="b2", dtype=tf.float32)
    self.trainable_weights = [self.V, self.W, self.b, self.b2]
    
  def create_tensor(self, in_layers=None, set_tensors=True, **kwargs):
    if in_layers is None:
      in_layers = self.in_layers
    in_layers = convert_to_layers(in_layers)

    self.build()
    inputs = in_layers[0].out_tensor
    K = self.K
    outputs = []
    for count in range(self.n_tasks):
      # Similarity values
      similarity = inputs[:, 2 * K * count:(2 * K * count + K)]
      # Labels for all top K similar samples
      ys = tf.to_int32(inputs[:, (2 * K * count + K):2 * K * (count + 1)])
      
      R = self.b + self.W[0] * similarity + self.W[1] * tf.constant(
          np.arange(K) + 1, dtype=tf.float32)
      R = tf.sigmoid(R)
      z = tf.reduce_sum(R * tf.gather(self.V, ys), axis=1) + self.b2
      outputs.append(tf.reshape(z, shape=[-1, 1]))
    out_tensor = tf.concat(outputs, axis=1)
    
    if set_tensors:
      self.variables = self.trainable_weights
      self.out_tensor = out_tensor
    return out_tensor

  def none_tensors(self):
    V, W, b, b2 = self.V, self.W, self.b, self.b2
    self.V, self.W, self.b, self.b2 = None, None, None, None

    out_tensor, trainable_weights, variables = self.out_tensor, self.trainable_weights, self.variables
    self.out_tensor, self.trainable_weights, self.variables = None, [], []
    return V, W, b, b2, out_tensor, trainable_weights, variables

  def set_tensors(self, tensor):
    self.V, self.W, self.b, self.b2, self.out_tensor, self.trainable_weights, self.variables = tensor

class IRVRegularize(Layer):
  """ This Layer extracts the trainable weights in IRVLayer
  and return the their L2-norm
  """
  def __init__(self,
               IRVLayer,
               penalty=0.0,
               **kwargs):
    self.IRVLayer = IRVLayer
    self.penalty = penalty
    super(IRVRegularize, self).__init__(**kwargs)
    
  def create_tensor(self, in_layers=None, set_tensors=True, **kwargs):
    assert self.IRVLayer.out_tensor is not None, "IRVLayer must be built first"
    out_tensor = tf.nn.l2_loss(self.IRVLayer.W) + \
        tf.nn.l2_loss(self.IRVLayer.V) + tf.nn.l2_loss(self.IRVLayer.b) + \
        tf.nn.l2_loss(self.IRVLayer.b2)
    out_tensor = out_tensor * self.penalty
    if set_tensors:
      self.out_tensor = out_tensor
    return out_tensor

class Slice(Layer):
  """ Choose a slice of input given axis and order
  """

  def __init__(self,
               slice_num,
               axis=1,
               **kwargs):
    """
    Parameters
    ----------
    slice_num: int
      index of slice number
    axis: int
      axis id
    """
    self.slice_num = slice_num
    self.axis = axis
    super(Slice, self).__init__(**kwargs)

  def create_tensor(self, in_layers=None, set_tensors=True, **kwargs):
    if in_layers is None:
      in_layers = self.in_layers
    in_layers = convert_to_layers(in_layers)

    slice_num = self.slice_num
    axis = self.axis
    inputs = in_layers[0].out_tensor
    out_tensor = tf.slice(inputs, [0]*axis+[slice_num], [-1]*axis+[1])
    
    if set_tensors:
      self.out_tensor = out_tensor
    return out_tensor

class TensorflowMultiTaskIRVClassifier(TensorGraph):

  def __init__(self,
               n_tasks,
               K=10,
               penalty=0.0,
               mode="classification",
               **kwargs):
    """Initialize TensorflowMultiTaskIRVClassifier
    
    Parameters
    ----------
    n_tasks: int
      Number of tasks
    K: int
      Number of nearest neighbours used in classification
    penalty: float
      Amount of penalty (l2 or l1 applied)
      
    """
    self.n_tasks = n_tasks
    self.K = K
    self.n_features = 2 * self.K * self.n_tasks
    print("n_features after fit_transform: %d" % int(self.n_features))
    self.penalty = penalty
    super(TensorflowMultiTaskIRVClassifier, self).__init__(**kwargs)
    self.build_graph()
    

  def build_graph(self):
    """Constructs the graph architecture of IRV as described in:
       
       https://www.ncbi.nlm.nih.gov/pmc/articles/PMC2750043/
    """
    self.mol_features = Feature(shape=(None, self.n_features))
    predictions = IRVLayer(self.n_tasks, self.K, in_layers=[self.mol_features])
    costs = []
    self.labels_fd = []
    for task in range(self.n_tasks):
      task_output = Slice(task, 1, in_layers=[predictions])
      sigmoid = Sigmoid(in_layers=[task_output])
      self.add_output(sigmoid)

      label = Label(shape=(None, 1))
      self.labels_fd.append(label)
      cost = SigmoidCrossEntropy(in_layers=[label, task_output])
      costs.append(cost)
    all_cost = Concat(in_layers=costs, axis=1)
    self.weights = Weights(shape=(None, self.n_tasks))
    loss = WeightedError(in_layers=[all_cost, self.weights]) + \
        IRVRegularize(predictions, self.penalty, in_layers=[predictions])
    self.set_loss(loss)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    """TensorGraph style implementation """
    for epoch in range(epochs):
      if not predict:
        print('Starting epoch %i' % epoch)
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=deterministic,
          pad_batches=pad_batches):

        feed_dict = dict()
        if y_b is not None:
          for index, label in enumerate(self.labels_fd):
            feed_dict[label] = y_b[:, index:index+1]
        if w_b is not None:
          feed_dict[self.weights] = w_b
        feed_dict[self.mol_features] = X_b

        yield feed_dict

  def predict(self, dataset, transformers=[], outputs=None):
    out = super(TensorflowMultiTaskIRVClassifier, self).predict(
        dataset, transformers=transformers, outputs=outputs)
    out = np.concatenate(out, axis=1)
    out = np.round(out).astype(int)
    return out

  def predict_proba(self, dataset, transformers=[], outputs=None):
    out = super(TensorflowMultiTaskIRVClassifier, self).predict_proba(
        dataset, transformers=transformers, outputs=outputs)
    out = np.concatenate(out, axis=1)
    out = np.stack([1-out, out], axis=2)
    return out