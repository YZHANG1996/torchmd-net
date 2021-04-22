import ase
import torch
from torch import nn
from torch.nn import functional as F

from torch_scatter import scatter
from torch_geometric.nn import radius_graph, MessagePassing

from torchmdnet.models.utils import NeighborEmbedding, CosineCutoff, rbf_class_mapping, act_class_mapping


class TorchMD_GN(nn.Module):
    r"""The TorchMD Graph Network architecture.
    Code adapted from https://github.com/rusty1s/pytorch_geometric/blob/d7d8e5e2edada182d820bbb1eec5f016f50db1e0/torch_geometric/nn/models/schnet.py#L38

    .. math::
        \mathbf{x}^{\prime}_i = \sum_{j \in \mathcal{N}(i)} \mathbf{x}_j \odot
        h_{\mathbf{\Theta}} ( \exp(-\gamma(\mathbf{e}_{j,i} - \mathbf{\mu}))),

    here :math:`h_{\mathbf{\Theta}}` denotes an MLP and
    :math:`\mathbf{e}_{j,i}` denotes the interatomic distances between atoms.

    Args:
        hidden_channels (int, optional): Hidden embedding size.
            (default: :obj:`128`)
        num_filters (int, optional): The number of filters to use.
            (default: :obj:`128`)
        num_interactions (int, optional): The number of interaction blocks.
            (default: :obj:`6`)
        num_rbf (int, optional): The number of radial basis functions :math:`\mu`.
            (default: :obj:`50`)
        rbf_type (string, optional): The type of radial basis function to use.
            (default: :obj:`"expnorm"`)
        trainable_rbf (bool, optional): Whether to train RBF parameters with
            backpropagation. (default: :obj:`True`)
        activation (string, optional): The type of activation function to use.
            (default: :obj:`"silu"`)
        neighbor_embedding (bool, optional): Whether to perform an initial neighbor
            embedding step. (default: :obj:`True`)
        cutoff_lower (float, optional): Lower cutoff distance for interatomic interactions.
            (default: :obj:`0.0`)
        cutoff_upper (float, optional): Upper cutoff distance for interatomic interactions.
            (default: :obj:`5.0`)
        readout (string, optional): Whether to apply :obj:`"add"` or
            :obj:`"mean"` global aggregation. (default: :obj:`"add"`)
        dipole (bool, optional): If set to :obj:`True`, will use the magnitude
            of the dipole moment to make the final prediction, *e.g.*, for
            target 0 of :class:`torch_geometric.datasets.QM9`.
            (default: :obj:`False`)
        mean (float, optional): The mean of the property to predict.
            (default: :obj:`None`)
        std (float, optional): The standard deviation of the property to
            predict. (default: :obj:`None`)
        atomref (torch.Tensor, optional): The reference of single-atom
            properties.
            Expects a vector of shape :obj:`(max_atomic_number, )`.
        derivative (bool, optional): If True, computes the derivative of the prediction
            w.r.t the input coordinates. (default: :obj:`False`)
        atom_filter (int, optional): Only sum over atoms with Z > atom_filter.
            (default: :obj:`0`)
    """

    def __init__(self, hidden_channels=128, num_filters=128,
                 num_interactions=6, num_rbf=50, rbf_type='expnorm',
                 trainable_rbf=True, activation='silu', neighbor_embedding=True,
                 cutoff_lower=0.0, cutoff_upper=5.0, readout='add', dipole=False,
                 mean=None, std=None, atomref=None, derivative=False, atom_filter=0):
        super(TorchMD_GN, self).__init__()

        assert readout in ['add', 'sum', 'mean']
        assert rbf_type in rbf_class_mapping, (f'Unknown RBF type "{rbf_type}". '
                                               f'Choose from {", ".join(rbf_class_mapping.keys())}.')
        assert activation in act_class_mapping, (f'Unknown activation function "{activation}". '
                                                 f'Choose from {", ".join(act_class_mapping.keys())}.')

        self.hidden_channels = hidden_channels
        self.num_filters = num_filters
        self.num_interactions = num_interactions
        self.num_rbf = num_rbf
        self.rbf_type = rbf_type
        self.trainable_rbf = trainable_rbf
        self.activation = activation
        self.neighbor_embedding = neighbor_embedding
        self.cutoff_lower = cutoff_lower
        self.cutoff_upper = cutoff_upper
        self.readout = 'add' if dipole else readout
        self.dipole = dipole
        self.mean = mean
        self.std = std
        self.derivative = derivative
        self.atom_filter = atom_filter

        atomic_mass = torch.from_numpy(ase.data.atomic_masses)
        self.register_buffer('atomic_mass', atomic_mass)

        act_class = act_class_mapping[activation]

        self.embedding = nn.Embedding(100, hidden_channels)
        self.distance_expansion = rbf_class_mapping[rbf_type](
            cutoff_lower, cutoff_upper, num_rbf, trainable_rbf
        )
        self.neighbor_embedding = NeighborEmbedding(
            hidden_channels, num_rbf, cutoff_lower, cutoff_upper
        ) if neighbor_embedding else None

        self.interactions = nn.ModuleList()
        for _ in range(num_interactions):
            block = InteractionBlock(hidden_channels, num_rbf, num_filters,
                                     act_class, cutoff_lower, cutoff_upper)
            self.interactions.append(block)

        self.lin1 = nn.Linear(hidden_channels, hidden_channels // 2)
        self.act = act_class()
        self.lin2 = nn.Linear(hidden_channels // 2, 1)

        self.register_buffer('initial_atomref', atomref)
        self.atomref = None
        if atomref is not None:
            self.atomref = nn.Embedding(100, 1)
            self.atomref.weight.data.copy_(atomref)

        self.reset_parameters()

    def reset_parameters(self):
        self.embedding.reset_parameters()
        for interaction in self.interactions:
            interaction.reset_parameters()
        nn.init.xavier_uniform_(self.lin1.weight)
        self.lin1.bias.data.fill_(0)
        nn.init.xavier_uniform_(self.lin2.weight)
        self.lin2.bias.data.fill_(0)
        if self.atomref is not None:
            self.atomref.weight.data.copy_(self.initial_atomref)

    def forward(self, z, pos, batch=None):
        assert z.dim() == 1 and z.dtype == torch.long
        batch = torch.zeros_like(z) if batch is None else batch

        if self.derivative:
            pos.requires_grad_(True)

        h = self.embedding(z)

        edge_index = radius_graph(pos, r=self.cutoff_upper, batch=batch)
        row, col = edge_index
        edge_weight = (pos[row] - pos[col]).norm(dim=-1)
        edge_attr = self.distance_expansion(edge_weight)

        if self.neighbor_embedding:
            h = self.neighbor_embedding(z, h, edge_index, edge_weight, edge_attr)

        for interaction in self.interactions:
            h = h + interaction(h, edge_index, edge_weight, edge_attr)

        # drop atoms according to the filter
        atom_mask = z > self.atom_filter
        h = h[atom_mask]
        z = z[atom_mask]
        pos = pos[atom_mask]
        batch = batch[atom_mask]

        # output network
        h = self.lin1(h)
        h = self.act(h)
        h = self.lin2(h)

        if self.dipole:
            # Get center of mass.
            mass = self.atomic_mass[z].view(-1, 1)
            c = scatter(mass * pos, batch, dim=0) / scatter(mass, batch, dim=0)
            h = h * (pos - c[batch])

        if not self.dipole and self.mean is not None and self.std is not None:
            h = h * self.std + self.mean

        if not self.dipole and self.atomref is not None:
            h = h + self.atomref(z)

        out = scatter(h, batch, dim=0, reduce=self.readout)

        if self.dipole:
            out = torch.norm(out, dim=-1, keepdim=True)

        if self.derivative:
            dy = -torch.autograd.grad(out, pos, grad_outputs=torch.ones_like(out),
                                      create_graph=True, retain_graph=True)[0]
            return out, dy

        return out

    def __repr__(self):
        return (f'{self.__class__.__name__}('
                f'hidden_channels={self.hidden_channels}, '
                f'num_filters={self.num_filters}, '
                f'num_interactions={self.num_interactions}, '
                f'num_rbf={self.num_rbf}, '
                f'rbf_type={self.rbf_type}, '
                f'trainable_rbf={self.trainable_rbf}, '
                f'activation={self.activation}, '
                f'neighbor_embedding={self.neighbor_embedding}, '
                f'cutoff_lower={self.cutoff_lower}, '
                f'cutoff_upper={self.cutoff_upper}, '
                f'derivative={self.derivative}, '
                f'atom_filter={self.atom_filter})')


class InteractionBlock(nn.Module):
    def __init__(self, hidden_channels, num_rbf, num_filters, activation,
                 cutoff_lower, cutoff_upper):
        super(InteractionBlock, self).__init__()
        self.mlp = nn.Sequential(
            nn.Linear(num_rbf, num_filters),
            activation(),
            nn.Linear(num_filters, num_filters),
        )
        self.conv = CFConv(hidden_channels, hidden_channels, num_filters,
                           self.mlp, cutoff_lower, cutoff_upper)
        self.act = activation()
        self.lin = nn.Linear(hidden_channels, hidden_channels)

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.mlp[0].weight)
        self.mlp[0].bias.data.fill_(0)
        nn.init.xavier_uniform_(self.mlp[2].weight)
        self.mlp[0].bias.data.fill_(0)
        self.conv.reset_parameters()
        nn.init.xavier_uniform_(self.lin.weight)
        self.lin.bias.data.fill_(0)

    def forward(self, x, edge_index, edge_weight, edge_attr):
        x = self.conv(x, edge_index, edge_weight, edge_attr)
        x = self.act(x)
        x = self.lin(x)
        return x


class CFConv(MessagePassing):
    def __init__(self, in_channels, out_channels, num_filters, net,
                 cutoff_lower, cutoff_upper):
        super(CFConv, self).__init__(aggr='add')
        self.lin1 = nn.Linear(in_channels, num_filters, bias=False)
        self.lin2 = nn.Linear(num_filters, out_channels)
        self.net = net
        self.cutoff = CosineCutoff(cutoff_lower, cutoff_upper)

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.lin1.weight)
        nn.init.xavier_uniform_(self.lin2.weight)
        self.lin2.bias.data.fill_(0)

    def forward(self, x, edge_index, edge_weight, edge_attr):
        C = self.cutoff(edge_weight)
        W = self.net(edge_attr) * C.view(-1, 1)

        x = self.lin1(x)
        x = self.propagate(edge_index, x=x, W=W)
        x = self.lin2(x)
        return x

    def message(self, x_j, W):
        return x_j * W