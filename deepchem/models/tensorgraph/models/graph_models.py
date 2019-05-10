import collections

import deepchem as dc
import numpy as np
import tensorflow as tf

from deepchem.data import NumpyDataset, pad_features
from deepchem.feat.graph_features import ConvMolFeaturizer
from deepchem.feat.mol_graphs import ConvMol
from deepchem.metrics import to_one_hot
from deepchem.models import KerasModel
import deepchem.models.layers as layers
from deepchem.models.tensorgraph.graph_layers import WeaveGather, \
    DTNNEmbedding, DTNNStep, DTNNGather, DAGLayer, \
    DAGGather, DTNNExtract, MessagePassing, SetGather
from deepchem.models.tensorgraph.graph_layers import WeaveLayerFactory
from deepchem.models.tensorgraph.layers import Layer, Dense, SoftMax, Reshape, \
    SoftMaxCrossEntropy, GraphConv, BatchNorm, Exp, ReduceMean, ReduceSum, \
    GraphPool, GraphGather, WeightedError, Dropout, BatchNorm, Stack, Flatten, GraphCNN, GraphCNNPool
from deepchem.models.tensorgraph.layers import L2Loss, Label, Weights, Feature
from deepchem.models.tensorgraph.tensor_graph import TensorGraph
from deepchem.trans import undo_transforms


class TrimGraphOutput(tf.keras.layers.Layer):
  """Trim the output to the correct number of samples.

  GraphGather always outputs fixed size batches.  This layer trims the output
  to the number of samples that were in the actual input tensors.
  """

  def __init__(self, **kwargs):
    super(TrimGraphOutput, self).__init__(**kwargs)

  def call(self, inputs):
    n_samples = tf.squeeze(inputs[1])
    return inputs[0][0:n_samples]


class WeaveModel(KerasModel):

  def __init__(self,
               n_tasks,
               n_atom_feat=75,
               n_pair_feat=14,
               n_hidden=50,
               n_graph_feat=128,
               mode="classification",
               n_classes=2,
               batch_size=100,
               **kwargs):
    """
    Parameters
    ----------
    n_tasks: int
      Number of tasks
    n_atom_feat: int, optional
      Number of features per atom.
    n_pair_feat: int, optional
      Number of features per pair of atoms.
    n_hidden: int, optional
      Number of units(convolution depths) in corresponding hidden layer
    n_graph_feat: int, optional
      Number of output features for each molecule(graph)
    mode: str
      Either "classification" or "regression" for type of model.
    n_classes: int
      Number of classes to predict (only used in classification mode)
    """
    if mode not in ['classification', 'regression']:
      raise ValueError("mode must be either 'classification' or 'regression'")
    self.n_tasks = n_tasks
    self.n_atom_feat = n_atom_feat
    self.n_pair_feat = n_pair_feat
    self.n_hidden = n_hidden
    self.n_graph_feat = n_graph_feat
    self.mode = mode
    self.n_classes = n_classes

    # Build the model.

    self.atom_features = tf.keras.Input(shape=(self.n_atom_feat,))
    self.pair_features = tf.keras.Input(shape=(self.n_pair_feat,))
    self.pair_split = tf.keras.Input(shape=tuple(), dtype=tf.int32)
    self.atom_split = tf.keras.Input(shape=tuple(), dtype=tf.int32)
    self.atom_to_pair = tf.keras.Input(shape=(2,), dtype=tf.int32)
    weave_layer1A, weave_layer1P = layers.WeaveLayer(
        n_atom_input_feat=self.n_atom_feat,
        n_pair_input_feat=self.n_pair_feat,
        n_atom_output_feat=self.n_hidden,
        n_pair_output_feat=self.n_hidden)([
            self.atom_features, self.pair_features, self.pair_split,
            self.atom_to_pair
        ])
    weave_layer2A, weave_layer2P = layers.WeaveLayer(
        n_atom_input_feat=self.n_hidden,
        n_pair_input_feat=self.n_hidden,
        n_atom_output_feat=self.n_hidden,
        n_pair_output_feat=self.n_hidden,
        update_pair=False)(
            [weave_layer1A, weave_layer1P, self.pair_split, self.atom_to_pair])
    dense1 = tf.keras.layers.Dense(
        self.n_graph_feat, activation=tf.nn.tanh)(weave_layer2A)
    batch_norm1 = tf.keras.layers.BatchNormalization(epsilon=1e-5)(dense1)
    weave_gather = layers.WeaveGather(
        batch_size, n_input=self.n_graph_feat,
        gaussian_expand=True)([batch_norm1, self.atom_split])

    n_tasks = self.n_tasks
    if self.mode == 'classification':
      n_classes = self.n_classes
      logits = tf.keras.layers.Reshape((n_tasks, n_classes))(
          tf.keras.layers.Dense(n_tasks * n_classes)(weave_gather))
      output = tf.keras.layers.Softmax()(logits)
      outputs = [output, logits]
      output_types = ['prediction', 'loss']
      loss = dc.models.losses.SoftmaxCrossEntropy()
    else:
      output = tf.keras.layers.Dense(n_tasks)(weave_gather)
      outputs = [output]
      output_types = ['prediction']
      loss = dc.models.losses.L2Loss()
    model = tf.keras.Model(
        inputs=[
            self.atom_features, self.pair_features, self.pair_split,
            self.atom_split, self.atom_to_pair
        ],
        outputs=outputs)
    super(WeaveModel, self).__init__(
        model, loss, output_types=output_types, batch_size=batch_size, **kwargs)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    for epoch in range(epochs):
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=deterministic,
          pad_batches=pad_batches):
        if y_b is not None:
          if self.mode == 'classification':
            y_b = to_one_hot(y_b.flatten(), self.n_classes).reshape(
                -1, self.n_tasks, self.n_classes)
        atom_feat = []
        pair_feat = []
        atom_split = []
        atom_to_pair = []
        pair_split = []
        start = 0
        for im, mol in enumerate(X_b):
          n_atoms = mol.get_num_atoms()
          # number of atoms in each molecule
          atom_split.extend([im] * n_atoms)
          # index of pair features
          C0, C1 = np.meshgrid(np.arange(n_atoms), np.arange(n_atoms))
          atom_to_pair.append(
              np.transpose(
                  np.array([C1.flatten() + start,
                            C0.flatten() + start])))
          # number of pairs for each atom
          pair_split.extend(C1.flatten() + start)
          start = start + n_atoms

          # atom features
          atom_feat.append(mol.get_atom_features())
          # pair features
          pair_feat.append(
              np.reshape(mol.get_pair_features(),
                         (n_atoms * n_atoms, self.n_pair_feat)))

        inputs = [
            np.concatenate(atom_feat, axis=0),
            np.concatenate(pair_feat, axis=0),
            np.array(pair_split),
            np.array(atom_split),
            np.concatenate(atom_to_pair, axis=0)
        ]
        yield (inputs, [y_b], [w_b])


class DTNNModel(KerasModel):

  def __init__(self,
               n_tasks,
               n_embedding=30,
               n_hidden=100,
               n_distance=100,
               distance_min=-1,
               distance_max=18,
               output_activation=True,
               mode="regression",
               dropout=0.0,
               **kwargs):
    """
    Parameters
    ----------
    n_tasks: int
      Number of tasks
    n_embedding: int, optional
      Number of features per atom.
    n_hidden: int, optional
      Number of features for each molecule after DTNNStep
    n_distance: int, optional
      granularity of distance matrix
      step size will be (distance_max-distance_min)/n_distance
    distance_min: float, optional
      minimum distance of atom pairs, default = -1 Angstorm
    distance_max: float, optional
      maximum distance of atom pairs, default = 18 Angstorm
    mode: str
      Only "regression" is currently supported.
    dropout: float
      the dropout probablity to use.
    """
    if mode not in ['regression']:
      raise ValueError("Only 'regression' mode is currently supported")
    self.n_tasks = n_tasks
    self.n_embedding = n_embedding
    self.n_hidden = n_hidden
    self.n_distance = n_distance
    self.distance_min = distance_min
    self.distance_max = distance_max
    self.step_size = (distance_max - distance_min) / n_distance
    self.steps = np.array(
        [distance_min + i * self.step_size for i in range(n_distance)])
    self.steps = np.expand_dims(self.steps, 0)
    self.output_activation = output_activation
    self.mode = mode
    self.dropout = dropout

    # Build the model.

    self.atom_number = tf.keras.Input(shape=tuple(), dtype=tf.int32)
    self.distance = tf.keras.Input(shape=(self.n_distance,))
    self.atom_membership = tf.keras.Input(shape=tuple(), dtype=tf.int32)
    self.distance_membership_i = tf.keras.Input(shape=tuple(), dtype=tf.int32)
    self.distance_membership_j = tf.keras.Input(shape=tuple(), dtype=tf.int32)

    dtnn_embedding = layers.DTNNEmbedding(n_embedding=self.n_embedding)(
        self.atom_number)
    if self.dropout > 0.0:
      dtnn_embedding = tf.keras.layers.Dropout(
          rate=self.dropout)(dtnn_embedding)
    dtnn_layer1 = layers.DTNNStep(
        n_embedding=self.n_embedding, n_distance=self.n_distance)([
            dtnn_embedding, self.distance, self.distance_membership_i,
            self.distance_membership_j
        ])
    if self.dropout > 0.0:
      dtnn_layer1 = tf.keras.layers.Dropout(rate=self.dropout)(dtnn_layer1)
    dtnn_layer2 = layers.DTNNStep(
        n_embedding=self.n_embedding, n_distance=self.n_distance)([
            dtnn_layer1, self.distance, self.distance_membership_i,
            self.distance_membership_j
        ])
    if self.dropout > 0.0:
      dtnn_layer2 = tf.keras.layers.Dropout(rate=self.dropout)(dtnn_layer2)
    dtnn_gather = layers.DTNNGather(
        n_embedding=self.n_embedding,
        layer_sizes=[self.n_hidden],
        n_outputs=self.n_tasks,
        output_activation=self.output_activation)(
            [dtnn_layer2, self.atom_membership])
    if self.dropout > 0.0:
      dtnn_gather = tf.keras.layers.Dropout(rate=self.dropout)(dtnn_gather)

    n_tasks = self.n_tasks
    output = tf.keras.layers.Dense(n_tasks)(dtnn_gather)
    model = tf.keras.Model(
        inputs=[
            self.atom_number, self.distance, self.atom_membership,
            self.distance_membership_i, self.distance_membership_j
        ],
        outputs=[output])
    super(DTNNModel, self).__init__(model, dc.models.losses.L2Loss(), **kwargs)

  def compute_features_on_batch(self, X_b):
    """Computes the values for different Feature Layers on given batch

    A tf.py_func wrapper is written around this when creating the
    input_fn for tf.Estimator

    """
    distance = []
    atom_membership = []
    distance_membership_i = []
    distance_membership_j = []
    num_atoms = list(map(sum, X_b.astype(bool)[:, :, 0]))
    atom_number = [
        np.round(
            np.power(2 * np.diag(X_b[i, :num_atoms[i], :num_atoms[i]]),
                     1 / 2.4)).astype(int) for i in range(len(num_atoms))
    ]
    start = 0
    for im, molecule in enumerate(atom_number):
      distance_matrix = np.outer(
          molecule, molecule) / X_b[im, :num_atoms[im], :num_atoms[im]]
      np.fill_diagonal(distance_matrix, -100)
      distance.append(np.expand_dims(distance_matrix.flatten(), 1))
      atom_membership.append([im] * num_atoms[im])
      membership = np.array([np.arange(num_atoms[im])] * num_atoms[im])
      membership_i = membership.flatten(order='F')
      membership_j = membership.flatten()
      distance_membership_i.append(membership_i + start)
      distance_membership_j.append(membership_j + start)
      start = start + num_atoms[im]

    atom_number = np.concatenate(atom_number).astype(np.int32)
    distance = np.concatenate(distance, axis=0)
    gaussian_dist = np.exp(
        -np.square(distance - self.steps) / (2 * self.step_size**2))
    gaussian_dist = gaussian_dist.astype(np.float32)
    atom_mem = np.concatenate(atom_membership).astype(np.int32)
    dist_mem_i = np.concatenate(distance_membership_i).astype(np.int32)
    dist_mem_j = np.concatenate(distance_membership_j).astype(np.int32)

    features = [atom_number, gaussian_dist, atom_mem, dist_mem_i, dist_mem_j]

    return features

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    for epoch in range(epochs):
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=deterministic,
          pad_batches=pad_batches):
        yield (self.compute_features_on_batch(X_b), [y_b], [w_b])


class DAGModel(KerasModel):

  def __init__(self,
               n_tasks,
               max_atoms=50,
               n_atom_feat=75,
               n_graph_feat=30,
               n_outputs=30,
               layer_sizes=[100],
               layer_sizes_gather=[100],
               dropout=None,
               mode="classification",
               n_classes=2,
               uncertainty=False,
               batch_size=100,
               **kwargs):
    """
    Parameters
    ----------
    n_tasks: int
      Number of tasks.
    max_atoms: int, optional
      Maximum number of atoms in a molecule, should be defined based on dataset.
    n_atom_feat: int, optional
      Number of features per atom.
    n_graph_feat: int, optional
      Number of features for atom in the graph.
    n_outputs: int, optional
      Number of features for each molecule.
    layer_sizes: list of int, optional
      List of hidden layer size(s) in the propagation step:
      length of this list represents the number of hidden layers,
      and each element is the width of corresponding hidden layer.
    layer_sizes_gather: list of int, optional
      List of hidden layer size(s) in the gather step.
    dropout: None or float, optional
      Dropout probability, applied after each propagation step and gather step.
    mode: str, optional
      Either "classification" or "regression" for type of model.
    n_classes: int
      the number of classes to predict (only used in classification mode)
    uncertainty: bool
      if True, include extra outputs and loss terms to enable the uncertainty
      in outputs to be predicted
    """
    if mode not in ['classification', 'regression']:
      raise ValueError("mode must be either 'classification' or 'regression'")
    self.n_tasks = n_tasks
    self.max_atoms = max_atoms
    self.n_atom_feat = n_atom_feat
    self.n_graph_feat = n_graph_feat
    self.n_outputs = n_outputs
    self.layer_sizes = layer_sizes
    self.layer_sizes_gather = layer_sizes_gather
    self.dropout = dropout
    self.mode = mode
    self.n_classes = n_classes
    self.uncertainty = uncertainty
    if uncertainty:
      if mode != "regression":
        raise ValueError("Uncertainty is only supported in regression mode")
      if dropout == 0.0:
        raise ValueError('Dropout must be included to predict uncertainty')

    # Build the model.

    self.atom_features = tf.keras.Input(shape=(self.n_atom_feat,))
    self.parents = tf.keras.Input(
        shape=(self.max_atoms, self.max_atoms), dtype=tf.int32)
    self.calculation_orders = tf.keras.Input(
        shape=(self.max_atoms,), dtype=tf.int32)
    self.calculation_masks = tf.keras.Input(
        shape=(self.max_atoms,), dtype=tf.bool)
    self.membership = tf.keras.Input(shape=tuple(), dtype=tf.int32)
    self.n_atoms = tf.keras.Input(shape=tuple(), dtype=tf.int32)
    dag_layer1 = layers.DAGLayer(
        n_graph_feat=self.n_graph_feat,
        n_atom_feat=self.n_atom_feat,
        max_atoms=self.max_atoms,
        layer_sizes=self.layer_sizes,
        dropout=self.dropout,
        batch_size=batch_size)([
            self.atom_features, self.parents, self.calculation_orders,
            self.calculation_masks, self.n_atoms
        ])
    dag_gather = layers.DAGGather(
        n_graph_feat=self.n_graph_feat,
        n_outputs=self.n_outputs,
        max_atoms=self.max_atoms,
        layer_sizes=self.layer_sizes_gather,
        dropout=self.dropout)([dag_layer1, self.membership])
    n_tasks = self.n_tasks
    if self.mode == 'classification':
      n_classes = self.n_classes
      logits = tf.keras.layers.Reshape((n_tasks, n_classes))(
          tf.keras.layers.Dense(n_tasks * n_classes)(dag_gather))
      output = tf.keras.layers.Softmax()(logits)
      outputs = [output, logits]
      output_types = ['prediction', 'loss']
      loss = dc.models.losses.SoftmaxCrossEntropy()
    else:
      output = tf.keras.layers.Dense(n_tasks)(dag_gather)
      if self.uncertainty:
        log_var = Dense(n_tasks)(dag_gather)
        var = tf.keras.layers.Activation(tf.exp)(log_var)
        outputs = [output, var, output, log_var]
        output_types = ['prediction', 'variance', 'loss', 'loss']

        def loss(outputs, labels, weights):
          diff = labels[0] - outputs[0]
          return tf.reduce_mean(diff * diff / tf.exp(outputs[1]) + outputs[1])
      else:
        outputs = [output]
        output_types = ['prediction']
        loss = dc.models.losses.L2Loss()
    model = tf.keras.Model(
        inputs=[
            self.atom_features, self.parents, self.calculation_orders,
            self.calculation_masks, self.membership, self.n_atoms
        ],
        outputs=outputs)
    super(DAGModel, self).__init__(
        model, loss, output_types=output_types, batch_size=batch_size, **kwargs)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    """TensorGraph style implementation"""
    for epoch in range(epochs):
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=deterministic,
          pad_batches=pad_batches):

        if y_b is not None and self.mode == 'classification':
          y_b = to_one_hot(y_b.flatten(), self.n_classes).reshape(
              -1, self.n_tasks, self.n_classes)

        atoms_per_mol = [mol.get_num_atoms() for mol in X_b]
        n_atoms = sum(atoms_per_mol)
        start_index = [0] + list(np.cumsum(atoms_per_mol)[:-1])

        atoms_all = []
        # calculation orders for a batch of molecules
        parents_all = []
        calculation_orders = []
        calculation_masks = []
        membership = []
        for idm, mol in enumerate(X_b):
          # padding atom features vector of each molecule with 0
          atoms_all.append(mol.get_atom_features())
          parents = mol.parents
          parents_all.extend(parents)
          calculation_index = np.array(parents)[:, :, 0]
          mask = np.array(calculation_index - self.max_atoms, dtype=bool)
          calculation_orders.append(calculation_index + start_index[idm])
          calculation_masks.append(mask)
          membership.extend([idm] * atoms_per_mol[idm])

        yield ([
            np.concatenate(atoms_all, axis=0),
            np.stack(parents_all, axis=0),
            np.concatenate(calculation_orders, axis=0),
            np.concatenate(calculation_masks, axis=0),
            np.array(membership),
            np.array(n_atoms)
        ], [y_b], [w_b])


class GraphConvModel(KerasModel):

  def __init__(self,
               n_tasks,
               graph_conv_layers=[64, 64],
               dense_layer_size=128,
               dropout=0.0,
               mode="classification",
               number_atom_features=75,
               n_classes=2,
               uncertainty=False,
               batch_size=100,
               **kwargs):
    """
    Parameters
    ----------
    n_tasks: int
      Number of tasks
    graph_conv_layers: list of int
      Width of channels for the Graph Convolution Layers
    dense_layer_size: int
      Width of channels for Atom Level Dense Layer before GraphPool
    dropout: list or float
      the dropout probablity to use for each layer.  The length of this list should equal
      len(graph_conv_layers)+1 (one value for each convolution layer, and one for the
      dense layer).  Alternatively this may be a single value instead of a list, in which
      case the same value is used for every layer.
    mode: str
      Either "classification" or "regression"
    number_atom_features: int
        75 is the default number of atom features created, but
        this can vary if various options are passed to the
        function atom_features in graph_features
    n_classes: int
      the number of classes to predict (only used in classification mode)
    uncertainty: bool
      if True, include extra outputs and loss terms to enable the uncertainty
      in outputs to be predicted
    """
    if mode not in ['classification', 'regression']:
      raise ValueError("mode must be either 'classification' or 'regression'")
    self.n_tasks = n_tasks
    self.mode = mode
    self.dense_layer_size = dense_layer_size
    self.graph_conv_layers = graph_conv_layers
    self.number_atom_features = number_atom_features
    self.n_classes = n_classes
    self.uncertainty = uncertainty
    if not isinstance(dropout, collections.Sequence):
      dropout = [dropout] * (len(graph_conv_layers) + 1)
    if len(dropout) != len(graph_conv_layers) + 1:
      raise ValueError('Wrong number of dropout probabilities provided')
    self.dropout = dropout
    if uncertainty:
      if mode != "regression":
        raise ValueError("Uncertainty is only supported in regression mode")
      if any(d == 0.0 for d in dropout):
        raise ValueError(
            'Dropout must be included in every layer to predict uncertainty')

    # Build the model.

    self.atom_features = tf.keras.Input(shape=(self.number_atom_features,))
    self.degree_slice = tf.keras.Input(shape=(2,), dtype=tf.int32)
    self.membership = tf.keras.Input(shape=tuple(), dtype=tf.int32)
    self.n_samples = tf.keras.Input(shape=tuple(), dtype=tf.int32)

    self.deg_adjs = []
    for i in range(0, 10 + 1):
      deg_adj = tf.keras.Input(shape=(i + 1,), dtype=tf.int32)
      self.deg_adjs.append(deg_adj)
    in_layer = self.atom_features
    for layer_size, dropout in zip(self.graph_conv_layers, self.dropout):
      gc1_in = [in_layer, self.degree_slice, self.membership] + self.deg_adjs
      gc1 = layers.GraphConv(layer_size, activation_fn=tf.nn.relu)(gc1_in)
      batch_norm1 = tf.keras.layers.BatchNormalization(fused=False)(gc1)
      if dropout > 0.0:
        batch_norm1 = tf.keras.layers.Dropout(rate=dropout)(batch_norm1)
      gp_in = [batch_norm1, self.degree_slice, self.membership] + self.deg_adjs
      in_layer = layers.GraphPool()(gp_in)
    dense = tf.keras.layers.Dense(
        self.dense_layer_size, activation=tf.nn.relu)(in_layer)
    batch_norm3 = tf.keras.layers.BatchNormalization(fused=False)(dense)
    if self.dropout[-1] > 0.0:
      batch_norm3 = tf.keras.layers.Dropout(rate=self.dropout[-1])(batch_norm3)
    self.neural_fingerprint = layers.GraphGather(
        batch_size=batch_size, activation_fn=tf.nn.tanh)(
            [batch_norm3, self.degree_slice, self.membership] + self.deg_adjs)

    n_tasks = self.n_tasks
    if self.mode == 'classification':
      n_classes = self.n_classes
      logits = tf.keras.layers.Reshape((n_tasks, n_classes))(
          tf.keras.layers.Dense(n_tasks * n_classes)(self.neural_fingerprint))
      logits = TrimGraphOutput()([logits, self.n_samples])
      output = tf.keras.layers.Softmax()(logits)
      outputs = [output, logits]
      output_types = ['prediction', 'loss']
      loss = dc.models.losses.SoftmaxCrossEntropy()
    else:
      output = tf.keras.layers.Dense(n_tasks)(self.neural_fingerprint)
      output = TrimGraphOutput()([output, self.n_samples])
      if self.uncertainty:
        log_var = tf.keras.layers.Dense(n_tasks)(self.neural_fingerprint)
        log_var = TrimGraphOutput()([log_var, self.n_samples])
        var = tf.keras.layers.Activation(tf.exp)(log_var)
        outputs = [output, var, output, log_var]
        output_types = ['prediction', 'variance', 'loss', 'loss']

        def loss(outputs, labels, weights):
          diff = labels[0] - outputs[0]
          return tf.reduce_mean(diff * diff / tf.exp(outputs[1]) + outputs[1])
      else:
        outputs = [output]
        output_types = ['prediction']
        loss = dc.models.losses.L2Loss()
    model = tf.keras.Model(
        inputs=[
            self.atom_features, self.degree_slice, self.membership,
            self.n_samples
        ] + self.deg_adjs,
        outputs=outputs)
    super(GraphConvModel, self).__init__(
        model, loss, output_types=output_types, batch_size=batch_size, **kwargs)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    for epoch in range(epochs):
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=deterministic,
          pad_batches=pad_batches):
        if self.mode == 'classification':
          y_b = to_one_hot(y_b.flatten(), self.n_classes).reshape(
              -1, self.n_tasks, self.n_classes)
        multiConvMol = ConvMol.agglomerate_mols(X_b)
        n_samples = np.array(X_b.shape[0])
        inputs = [
            multiConvMol.get_atom_features(), multiConvMol.deg_slice,
            np.array(multiConvMol.membership), n_samples
        ]
        for i in range(1, len(multiConvMol.get_deg_adjacency_lists())):
          inputs.append(multiConvMol.get_deg_adjacency_lists()[i])
        yield (inputs, [y_b], [w_b])


class MPNNModel(TensorGraph):
  """ Message Passing Neural Network,
      default structures built according to https://arxiv.org/abs/1511.06391 """

  def __init__(self,
               n_tasks,
               n_atom_feat=70,
               n_pair_feat=8,
               n_hidden=100,
               T=5,
               M=10,
               mode="regression",
               dropout=0.0,
               n_classes=2,
               uncertainty=False,
               **kwargs):
    """
    Parameters
    ----------
    n_tasks: int
      Number of tasks
    n_atom_feat: int, optional
      Number of features per atom.
    n_pair_feat: int, optional
      Number of features per pair of atoms.
    n_hidden: int, optional
      Number of units(convolution depths) in corresponding hidden layer
    n_graph_feat: int, optional
      Number of output features for each molecule(graph)
    dropout: float
      the dropout probablity to use.
    n_classes: int
      the number of classes to predict (only used in classification mode)
    uncertainty: bool
      if True, include extra outputs and loss terms to enable the uncertainty
      in outputs to be predicted
    """
    if mode not in ['classification', 'regression']:
      raise ValueError("mode must be either 'classification' or 'regression'")
    self.n_tasks = n_tasks
    self.n_atom_feat = n_atom_feat
    self.n_pair_feat = n_pair_feat
    self.n_hidden = n_hidden
    self.T = T
    self.M = M
    self.mode = mode
    self.n_classes = n_classes
    self.uncertainty = uncertainty
    if uncertainty:
      if mode != "regression":
        raise ValueError("Uncertainty is only supported in regression mode")
      if dropout == 0.0:
        raise ValueError('Dropout must be included to predict uncertainty')
    super(MPNNModel, self).__init__(**kwargs)
    self.build_graph()

  def build_graph(self):
    # Build placeholders
    self.atom_features = Feature(shape=(None, self.n_atom_feat))
    self.pair_features = Feature(shape=(None, self.n_pair_feat))
    self.atom_split = Feature(shape=(None,), dtype=tf.int32)
    self.atom_to_pair = Feature(shape=(None, 2), dtype=tf.int32)

    message_passing = MessagePassing(
        self.T,
        message_fn='enn',
        update_fn='gru',
        n_hidden=self.n_hidden,
        in_layers=[self.atom_features, self.pair_features, self.atom_to_pair])

    atom_embeddings = Dense(self.n_hidden, in_layers=[message_passing])

    mol_embeddings = SetGather(
        self.M,
        self.batch_size,
        n_hidden=self.n_hidden,
        in_layers=[atom_embeddings, self.atom_split])

    dense1 = Dense(
        out_channels=2 * self.n_hidden,
        activation_fn=tf.nn.relu,
        in_layers=[mol_embeddings])

    n_tasks = self.n_tasks
    weights = Weights(shape=(None, n_tasks))
    if self.mode == 'classification':
      n_classes = self.n_classes
      labels = Label(shape=(None, n_tasks, n_classes))
      logits = Reshape(
          shape=(None, n_tasks, n_classes),
          in_layers=[Dense(in_layers=dense1, out_channels=n_tasks * n_classes)])
      logits = TrimGraphOutput([logits, weights])
      output = SoftMax(logits)
      self.add_output(output)
      loss = SoftMaxCrossEntropy(in_layers=[labels, logits])
      weighted_loss = WeightedError(in_layers=[loss, weights])
      self.set_loss(weighted_loss)
    else:
      labels = Label(shape=(None, n_tasks))
      output = Reshape(
          shape=(None, n_tasks),
          in_layers=[Dense(in_layers=dense1, out_channels=n_tasks)])
      output = TrimGraphOutput([output, weights])
      self.add_output(output)
      if self.uncertainty:
        log_var = Reshape(
            shape=(None, n_tasks),
            in_layers=[Dense(in_layers=dense1, out_channels=n_tasks)])
        log_var = TrimGraphOutput([log_var, weights])
        var = Exp(log_var)
        self.add_variance(var)
        diff = labels - output
        weighted_loss = weights * (diff * diff / var + log_var)
        weighted_loss = ReduceSum(ReduceMean(weighted_loss, axis=[1]))
      else:
        weighted_loss = ReduceSum(L2Loss(in_layers=[labels, output, weights]))
      self.set_loss(weighted_loss)

  def default_generator(self,
                        dataset,
                        epochs=1,
                        predict=False,
                        deterministic=True,
                        pad_batches=True):
    """ Same generator as Weave models """
    for epoch in range(epochs):
      for (X_b, y_b, w_b, ids_b) in dataset.iterbatches(
          batch_size=self.batch_size,
          deterministic=deterministic,
          pad_batches=False):

        X_b = pad_features(self.batch_size, X_b)
        feed_dict = dict()
        if y_b is not None:
          if self.mode == 'classification':
            feed_dict[self.labels[0]] = to_one_hot(y_b.flatten(),
                                                   self.n_classes).reshape(
                                                       -1, self.n_tasks,
                                                       self.n_classes)
          else:
            feed_dict[self.labels[0]] = y_b
        if w_b is not None:
          feed_dict[self.task_weights[0]] = w_b

        atom_feat = []
        pair_feat = []
        atom_split = []
        atom_to_pair = []
        pair_split = []
        start = 0
        for im, mol in enumerate(X_b):
          n_atoms = mol.get_num_atoms()
          # number of atoms in each molecule
          atom_split.extend([im] * n_atoms)
          # index of pair features
          C0, C1 = np.meshgrid(np.arange(n_atoms), np.arange(n_atoms))
          atom_to_pair.append(
              np.transpose(
                  np.array([C1.flatten() + start,
                            C0.flatten() + start])))
          # number of pairs for each atom
          pair_split.extend(C1.flatten() + start)
          start = start + n_atoms

          # atom features
          atom_feat.append(mol.get_atom_features())
          # pair features
          pair_feat.append(
              np.reshape(mol.get_pair_features(),
                         (n_atoms * n_atoms, self.n_pair_feat)))

        feed_dict[self.atom_features] = np.concatenate(atom_feat, axis=0)
        feed_dict[self.pair_features] = np.concatenate(pair_feat, axis=0)
        feed_dict[self.atom_split] = np.array(atom_split)
        feed_dict[self.atom_to_pair] = np.concatenate(atom_to_pair, axis=0)
        yield feed_dict


#################### Deprecation warnings for renamed TensorGraph models ####################

import warnings

TENSORGRAPH_DEPRECATION = "{} is deprecated and has been renamed to {} and will be removed in DeepChem 3.0."


class GraphConvTensorGraph(GraphConvModel):

  def __init__(self, *args, **kwargs):

    warnings.warn(
        TENSORGRAPH_DEPRECATION.format("GraphConvTensorGraph",
                                       "GraphConvModel"), FutureWarning)

    super(GraphConvTensorGraph, self).__init__(*args, **kwargs)


class WeaveTensorGraph(WeaveModel):

  def __init__(self, *args, **kwargs):

    warnings.warn(
        TENSORGRAPH_DEPRECATION.format("WeaveTensorGraph", "WeaveModel"),
        FutureWarning)

    super(WeaveModel, self).__init__(*args, **kwargs)


class DTNNTensorGraph(DTNNModel):

  def __init__(self, *args, **kwargs):

    warnings.warn(
        TENSORGRAPH_DEPRECATION.format("DTNNTensorGraph", "DTNNModel"),
        FutureWarning)

    super(DTNNModel, self).__init__(*args, **kwargs)


class DAGTensorGraph(DAGModel):

  def __init__(self, *args, **kwargs):

    warnings.warn(
        TENSORGRAPH_DEPRECATION.format("DAGTensorGraph", "DAGModel"),
        FutureWarning)

    super(DAGModel, self).__init__(*args, **kwargs)


class MPNNTensorGraph(MPNNModel):

  def __init__(self, *args, **kwargs):

    warnings.warn(
        TENSORGRAPH_DEPRECATION.format("MPNNTensorGraph", "MPNNModel"),
        FutureWarning)

    super(MPNNModel, self).__init__(*args, **kwargs)
