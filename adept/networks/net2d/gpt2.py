import copy
import math

import torch
from torch import nn
from torch.nn import Parameter

from adept.networks.net2d.submodule_2d import SubModule2D

# N_CTX  Sequence Len S 1024
# N_EMBD F


class GPT2(SubModule2D):
    args = {
        'nb_layer': 12,
        'nb_head': 12,
        'layer_norm_eps': 1e-5
    }

    def __init__(
            self,
            input_shape,
            id,
            nb_layer,
            nb_head,
            layer_norm_eps
    ):
        super(GPT2, self).__init__(input_shape, id)
        seq_len = input_shape[-2]
        feat_len = input_shape[-1]
        block = Block(seq_len, feat_len, nb_head, layer_norm_eps, scale=True)
        self.nb_layer = nb_layer
        self.h = nn.ModuleList(
            [copy.deepcopy(block) for _ in range(nb_layer)]
        )
        self.ln_f = LayerNorm(feat_len, eps=layer_norm_eps)
        self.apply(self._init_weights)

    @classmethod
    def from_args(cls, args, input_shape, id):
        return cls(
            input_shape,
            id,
            args.nb_layer,
            args.nb_head,
            args.layer_norm_eps
        )

    @property
    def _output_shape(self):
        return self.input_shape

    def _forward(self, input, internals, **kwargs):
        x = input
        new_internals = []
        for i in range(self.nb_layer):
            x, internal = self.h[i].forward(x, internals[i])
            new_internals.append(internal)
        new_internals = dict(enumerate(new_internals))
        x = self.ln_f(x)
        return x, new_internals

    def _new_internals(self):
        return dict(enumerate([None, ] * self.nb_layer))

    def _init_weights(self, module):
        """ Initialize the weights.
        """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=0.02)
        elif isinstance(module, LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()


class Block(nn.Module):
    def __init__(self, seq_len, feat_len, nb_head, layer_norm_eps, scale=False):
        super(Block, self).__init__()
        self.ln_1 = LayerNorm(feat_len, eps=layer_norm_eps)
        self.attn = Attention(feat_len, seq_len, nb_head, scale)
        self.ln_2 = LayerNorm(feat_len, eps=layer_norm_eps)
        self.mlp = MLP(4 * feat_len, feat_len)

    def forward(self, x, layer_past=None):
        a, present = self.attn(self.ln_1(x), layer_past=layer_past)
        x = x + a
        m = self.mlp(self.ln_2(x))
        x = x + m
        return x, present


class LayerNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-12):
        """Construct a layernorm module in the TF style (epsilon inside the square root).
        """
        super(LayerNorm, self).__init__()
        self.weight = nn.Parameter(torch.ones(hidden_size))
        self.bias = nn.Parameter(torch.zeros(hidden_size))
        self.variance_epsilon = eps

    def forward(self, x):
        u = x.mean(-1, keepdim=True)
        s = (x - u).pow(2).mean(-1, keepdim=True)
        x = (x - u) / torch.sqrt(s + self.variance_epsilon)
        return self.weight * x + self.bias


class Attention(nn.Module):
    def __init__(self, feat_len, seq_len, nb_head, scale=False):
        super(Attention, self).__init__()
        # [switch nx => n_state from Block to Attention to keep identical to TF implem]
        assert feat_len % nb_head == 0
        self.register_buffer("bias", torch.tril(torch.ones(seq_len, seq_len)).view(1, 1, seq_len, seq_len))
        self.n_head = nb_head
        self.split_size = feat_len
        self.scale = scale
        self.c_attn = Conv1D(feat_len * 3, feat_len)
        self.c_proj = Conv1D(feat_len, feat_len)

    def _attn(self, q, k, v):
        w = torch.matmul(q, k)
        if self.scale:
            w = w / math.sqrt(v.size(-1))
        nd, ns = w.size(-2), w.size(-1)
        b = self.bias[:, :, ns-nd:ns, :ns]
        w = w * b - 1e10 * (1 - b)

        w = nn.Softmax(dim=-1)(w)
        return torch.matmul(w, v)

    def merge_heads(self, x):
        x = x.permute(0, 2, 1, 3).contiguous()
        new_x_shape = x.size()[:-2] + (x.size(-2) * x.size(-1),)
        return x.view(*new_x_shape)  # in Tensorflow implem: fct merge_states

    def split_heads(self, x, k=False):
        new_x_shape = x.size()[:-1] + (self.n_head, x.size(-1) // self.n_head)
        x = x.view(*new_x_shape)  # in Tensorflow implem: fct split_states
        if k:
            return x.permute(0, 2, 3, 1)  # (batch, head, head_features, seq_length)
        else:
            return x.permute(0, 2, 1, 3)  # (batch, head, seq_length, head_features)

    def forward(self, x, layer_past=None):
        """
        :param x: torch.Tensor of shape (B, S, F)
        :param layer_past: torch.Tensor of previous key and value (2, B, S, F)
        :return:
        """
        x = self.c_attn(x)
        query, key, value = x.split(self.split_size, dim=2)
        query = self.split_heads(query)
        key = self.split_heads(key, k=True)
        value = self.split_heads(value)
        if layer_past is not None:
            past_key, past_value = layer_past[0].transpose(-2, -1), layer_past[1]  # transpose back cf below
            key = torch.cat((past_key, key), dim=-1)
            value = torch.cat((past_value, value), dim=-2)
        present = torch.stack((key.transpose(-2, -1), value))  # transpose to have same shapes for stacking
        a = self._attn(query, key, value)
        a = self.merge_heads(a)
        a = self.c_proj(a)
        return a, present


class MLP(nn.Module):
    def __init__(self, n_state, feat_len):  # in MLP: n_state=3072 (4 * n_embd)
        super(MLP, self).__init__()
        self.c_fc = Conv1D(n_state, feat_len)
        self.c_proj = Conv1D(feat_len, n_state)
        self.act = gelu

    def forward(self, x):
        h = self.act(self.c_fc(x))
        h2 = self.c_proj(h)
        return h2


class Conv1D(nn.Module):
    def __init__(self, nf, nx):
        super(Conv1D, self).__init__()
        self.nf = nf
        w = torch.empty(nx, nf)
        nn.init.normal_(w, std=0.02)
        self.weight = Parameter(w)
        self.bias = Parameter(torch.zeros(nf))

    def forward(self, x):
        size_out = x.size()[:-1] + (self.nf,)
        x = torch.addmm(self.bias, x.view(-1, x.size(-1)), self.weight)
        x = x.view(*size_out)
        return x


def gelu(x):
    return 0.5 * x * (1 + torch.tanh(math.sqrt(2 / math.pi) *
        (x + 0.044715 * torch.pow(x, 3))))