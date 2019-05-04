import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from torch.utils.data import DataLoader

class KGEModel(nn.Module):
    def __init__(self, model_name, nentity, nrelation, hidden_dim, gamma,
                 double_entity_embedding=False, double_relation_embedding=False,
                 node_attributes=[], pred_attributes=[],
                 attr_loss_to_graph_loss=1.0, device='cuda'):
        super(KGEModel, self).__init__()
        self.model_name = model_name
        self.nentity = nentity
        self.nrelation = nrelation
        self.hidden_dim = hidden_dim
        self.epsilon = 2.0
        self.attr_loss_to_graph_loss = attr_loss_to_graph_loss
        self.device = device

        self.gamma = nn.Parameter(torch.Tensor([gamma]), requires_grad=False)

        self.embedding_range = nn.Parameter(
            torch.Tensor([(self.gamma.item() + self.epsilon) / hidden_dim]),
            requires_grad=False)

        self.entity_dim = hidden_dim*2 if double_entity_embedding else hidden_dim
        self.relation_dim = hidden_dim*2 if double_relation_embedding else hidden_dim

        self.entity_embedding = nn.Parameter(torch.zeros(nentity, self.entity_dim))
        nn.init.uniform_(tensor=self.entity_embedding,
            a=-self.embedding_range.item(),
            b=self.embedding_range.item())

        self.relation_embedding = nn.Parameter(torch.zeros(nrelation, self.relation_dim))
        nn.init.uniform_(tensor=self.relation_embedding,
            a = -self.embedding_range.item(),
            b = self.embedding_range.item())
        
        self.attribute_layers = []
        self.node_attributes = node_attributes
        self.num_node_attributes = len(node_attributes)
        for i in range(0, self.num_node_attributes):
            # TODO: some initialization on these attribute layers
            self.attribute_layers.append(nn.Linear(self.entity_dim, 1))
            self.attribute_layers[-1].weight.requires_grad = False
            self.attribute_layers[-1].bias.requires_grad = False
            self.attribute_layers[-1].to(self.device)
        self.pred_attributes = pred_attributes
        self.num_pred_attributes = len(pred_attributes)
        for i in range(0, self.num_pred_attributes):
            # TODO: some initialization on these attribute layers
            self.attribute_layers.append(nn.Linear(self.relation_dim, 1))
            self.attribute_layers[-1].weight.requires_grad = False
            self.attribute_layers[-1].bias.requires_grad = False
            self.attribute_layers[-1].to(self.device)
        self.attr_loss_fn = nn.SmoothL1Loss()
        self.nonlinearity = torch.tanh # Cannot use relu since layers non-trainable: could start and stay negative only

        if model_name not in ['ComplEx', 'RotatE']:
            raise ValueError('model {} not supported'.format(model_name))

    def run_embedding(self, embedding, attribute_name):
        x = self.attribute_layers[self.node_attributes.index(attribute_name)](embedding)
        x = self.nonlinearity(x)
        return x.item()

    def forward(self, sample, mode='single', attributes=True):
        """A single forward pass"""

        if mode == 'single':
            batch_size, negative_sample_size = sample.size(0), 1

            head = torch.index_select(
                self.entity_embedding,
                dim=0,
                index=sample[:,0]
            ).unsqueeze(1)

            relation = torch.index_select(
                self.relation_embedding,
                dim=0,
                index=sample[:,1]
            ).unsqueeze(1)

            tail = torch.index_select(
                self.entity_embedding,
                dim=0,
                index=sample[:,2]
            ).unsqueeze(1)

            attr = sample[:, 3:]
            attr = attr.to(torch.float)

        elif mode == 'head-batch':
            tail_part, head_part = sample
            batch_size, negative_sample_size = head_part.size(0), head_part.size(1)

            attr = head_part[:, 3:]
            attr = attr.to(torch.float)
            head_part = head_part.to(torch.long)
            tail_part = tail_part.to(torch.long)

            head = torch.index_select(
                self.entity_embedding,
                dim=0,
                index=head_part.view(-1)
            ).view(batch_size, negative_sample_size, -1)

            relation = torch.index_select(
                self.relation_embedding,
                dim=0,
                index=tail_part[:, 1]
            ).unsqueeze(1)

            tail = torch.index_select(
                self.entity_embedding,
                dim=0,
                index=tail_part[:, 2]
            ).unsqueeze(1)

        elif mode == 'tail-batch':
            head_part, tail_part = sample
            batch_size, negative_sample_size = tail_part.size(0), tail_part.size(1)
            
            attr = head_part[:, 3:]
            head_part = head_part.to(torch.long)
            tail_part = tail_part.to(torch.long)
            attr = attr.to(torch.float)

            head = torch.index_select(
                self.entity_embedding,
                dim=0,
                index=head_part[:, 0]
            ).unsqueeze(1)

            relation = torch.index_select(
                self.relation_embedding,
                dim=0,
                index=head_part[:, 1]
            ).unsqueeze(1)

            tail = torch.index_select(
                self.entity_embedding,
                dim=0,
                index=tail_part.view(-1)
            ).view(batch_size, negative_sample_size, -1)

        model_func = {
            'ComplEx': self.ComplEx,
            'RotatE': self.RotatE
        }

        score = model_func[self.model_name](head, relation, tail, mode)

        if not attributes:
            return score, None

        attr_loss = torch.tensor(0, dtype=torch.float, device=self.device)
        for i, layer in enumerate(self.attribute_layers):
            attr_pred = layer(head)
            attr_pred = self.nonlinearity(attr_pred)
            attr_loss += self.attr_loss_to_graph_loss * self.attr_loss_fn(attr_pred, attr[:, i])

        return score, attr_loss

    def ComplEx(self, head, relation, tail, mode):
        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_relation, im_relation = torch.chunk(relation, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        if mode == 'head-batch':
            re_score = re_relation * re_tail + im_relation * im_tail
            im_score = re_relation * im_tail - im_relation * re_tail
            score = re_head * re_score + im_head * im_score
        else:
            re_score = re_head * re_relation - im_head * im_relation
            im_score = re_head * im_relation + im_head * re_relation
            score = re_score * re_tail + im_score * im_tail

        score = score.sum(dim = 2)
        return score

    def RotatE(self, head, relation, tail, mode):

        re_head, im_head = torch.chunk(head, 2, dim=2)
        re_tail, im_tail = torch.chunk(tail, 2, dim=2)

        phase_relation = relation / (self.embedding_range.item()/math.pi)

        re_relation = torch.cos(phase_relation)
        im_relation = torch.sin(phase_relation)

        if mode == 'head-batch':
            re_score = re_relation * re_tail + im_relation * im_tail
            im_score = re_relation * im_tail - im_relation * re_tail
            re_score = re_score - re_head
            im_score = im_score - im_head
        else:
            re_score = re_head * re_relation - im_head * im_relation
            im_score = re_head * im_relation + im_head * re_relation
            re_score = re_score - re_tail
            im_score = im_score - im_tail

        score = torch.stack([re_score, im_score], dim = 0)
        score = score.norm(dim = 0)

        score = self.gamma.item() - score.sum(dim=2)
        return score

    @staticmethod
    def train_step(model, optimizer, train_iterator, args):
        model.train()
        optimizer.zero_grad()
        
        positive_sample, negative_sample, subsampling_weight, mode = next(train_iterator)

        if args['cuda']:
            positive_sample = positive_sample.cuda()
            negative_sample = negative_sample.cuda()
            subsampling_weight = subsampling_weight.cuda()
        negative_score, _ = model((positive_sample, negative_sample), mode=mode)

        negative_score = F.logsigmoid(-negative_score).mean(dim=1)

        positive_score, attr_loss = model(positive_sample)
        positive_score = F.logsigmoid(positive_score).squeeze(dim=1)

        positive_sample_loss = - (subsampling_weight * positive_score).sum()/subsampling_weight.sum()
        negative_sample_loss = - (subsampling_weight * negative_score).sum()/subsampling_weight.sum()

        loss = (positive_sample_loss + negative_sample_loss) / 2
        loss += attr_loss

        loss.backward()
        optimizer.step()

        stats = {
            'pos_loss': round(positive_sample_loss.item(), 6),
            'neg_loss': round(negative_sample_loss.item(), 6),
            'loss': round(loss.item(), 6),
            'attr_loss': round(float(attr_loss), 4)
        }
        return stats
