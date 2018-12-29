#!/usr/bin/env python3

import torch
import itertools
from collections import defaultdict
from .lazy_tensor import LazyTensor
from .non_lazy_tensor import NonLazyTensor


class CatLazyTensor(LazyTensor):
    """
    A `LazyTensor` that represents the concatenation of `LazyTensor`s.
    Each LazyTensor must have the same shape except in the concatenating
    dimension.

    Args:
        - :attr:`lazy_tensors` (list of LazyTensors):
            A list of LazyTensors whose sizes are the same except in
            concatenating dimension :attr:`dim`.
        - :attr:`dim` (int):
            The concatenating dimension which can be a batch dimension.
    """

    def __init__(self, *lazy_tensors, dim=0, output_device=None):
        if len(lazy_tensors) == 0:
            raise RuntimeError("List of LazyTensors must be non-empty")
        if not all([isinstance(t, LazyTensor) for t in lazy_tensors]):
            raise RuntimeError("CatLazyTensor requires a list of all LazyTensors")
        if len(lazy_tensors) == 1:
            dim = 0

        super().__init__(*lazy_tensors, dim=dim, output_device=output_device)

        def remove_dim(tuple, dim):
            return tuple[:dim] + tuple[dim + 1:]

        rep_tensor = lazy_tensors[0]
        ndims = rep_tensor.ndimension()
        if dim < 0:
            dim = ndims + dim
        pre_cat_size = tuple(rep_tensor.size()[:dim])
        post_cat_size = tuple(rep_tensor.size()[dim + 1:])

        cat_dim_len = 0
        cat_dim_sizes = []
        tensor_idx_to_start_idx = []
        for t_idx, t in enumerate(lazy_tensors):
            if t.ndimension() != ndims:
                raise RuntimeError("All tensors must have the same number of dimensions")
            if remove_dim(t.size(), dim) != remove_dim(rep_tensor.size(), dim):
                raise RuntimeError("All LazyTensors must have the same size in "
                                   "the non-concatenation dimension")
            tensor_idx_to_start_idx.append(cat_dim_len)
            cat_dim_size = t.size()[dim]
            cat_dim_len += cat_dim_size
            cat_dim_sizes.append(cat_dim_size)

        # using itertools to more quickly join list of lists
        idx_to_tensor_idx = [[t_idx] * size for t_idx, size in enumerate(cat_dim_sizes)]
        idx_to_tensor_idx = list(itertools.chain.from_iterable(idx_to_tensor_idx))

        self.lazy_tensors = lazy_tensors
        self.pre_cat_size = pre_cat_size
        self.post_cat_size = post_cat_size
        self.cat_dim_sizes = cat_dim_sizes
        self.cat_dim_len = cat_dim_len
        # can't call this attribute self.dim because LazyTensor has a dim() function
        self.cat_dim = dim
        self.idx_to_tensor_idx = idx_to_tensor_idx
        self.tensor_idx_to_start_idx = tensor_idx_to_start_idx
        self.tensor_idx_to_end_idx = tensor_idx_to_start_idx[1:] + [sum(self.cat_dim_sizes)]
        self.output_device = output_device

    def _split_slice(self, slice_idx):
        """
        Splits a slice(a, b, None) in to a list of slices [slice(a1, b1, None), slice(a2, b2, None), ...]
        so that each slice in the list slices in to a single tensor that we have concatenated with this LazyTensor.
        """
        if slice_idx.step is not None:
            # TODO: Add support for this eventually.
            raise RuntimeError('Slicing a CatLazyTensor with a step is not currently supported!')
        start_idx = slice_idx.start if slice_idx.start is not None else 0
        stop_idx = slice_idx.stop if slice_idx.stop is not None else self.shape[self.cat_dim]

        start_tensor_idx = self.idx_to_tensor_idx[start_idx]
        stop_tensor_idx = self.idx_to_tensor_idx[stop_idx]

        if start_tensor_idx != stop_tensor_idx:
            # By definition, stop is on a later tensor than start since they are in order.
            end_idx = self.tensor_idx_to_end_idx[start_tensor_idx]
            my_slice = slice(start_idx, end_idx)

            if end_idx == stop_idx:
                return [my_slice]
            else:
                # Keep splitting
                return [my_slice] + self._split_slice(slice(end_idx, stop_idx, None))
        else:
            return [slice(start_idx, stop_idx, None)]

    def _getitem(self, *indices):
        squeeze = [isinstance(i, int) for i in indices]
        indices = list(indices)
        target_indices = indices[self.cat_dim]
        new_cat_dim = (None if squeeze[self.cat_dim]
                       else self.cat_dim - sum(squeeze[:self.cat_dim + 1]))

        if new_cat_dim is None:
            # target_indices must be a int so we let the LazyTensor squeeze out cat_dim
            t_idx = self.idx_to_tensor_idx[target_indices]
            return self.lazy_tensors[t_idx]._getitem(*indices)

        if all(torch.is_tensor(x) for x in indices):
            left_indices, right_indices = indices[-2], indices[-1]
            batch_indices = indices[:-2]
            return self._lazify(self._get_indices(left_indices, right_indices, *batch_indices))

        if isinstance(target_indices, slice):
            if target_indices == slice(None, None, None):
                res_list = [self._lazify(t._getitem(*indices)) for t in self.lazy_tensors]
                return self.__class__(*res_list, dim=new_cat_dim)
            else:
                target_slices = self._split_slice(target_indices)
                target_tensors = [self.idx_to_tensor_idx[sl.start] for sl in target_slices]

                res_list = []
                for idx, t_idx in zip(target_slices, target_tensors):
                    shifted_start = idx.start - self.tensor_idx_to_start_idx[t_idx]
                    shifted_stop = idx.stop - self.tensor_idx_to_start_idx[t_idx]
                    shifted_slice = slice(shifted_start, shifted_stop, idx.step)
                    indices[self.cat_dim] = shifted_slice
                    res = self._lazify(self.lazy_tensors[t_idx]._getitem(*indices))
                    res_list.append(res)
                return self.__class__(*res_list, dim=new_cat_dim)
        elif torch.is_tensor(target_indices):
            # this means another `indices` is a slice object
            target_indices = [idx.item() for idx in target_indices]
            target_tensors = [self.idx_to_tensor_idx[idx] for idx in target_indices]

            res_list = []
            curr_tensor, slice_indices = target_tensors[0], []
            for idx, t_idx in zip(target_indices, target_tensors):
                if t_idx != curr_tensor:
                    indices[self.cat_dim] = torch.tensor(slice_indices)
                    res = self._lazify(self.lazy_tensors[t_idx]._getitem(*indices))
                    res_list.append(res)
                    curr_tensor, slice_indices = t_idx, []
                slice_indices.append(idx - self.tensor_idx_to_start_idx[t_idx])
            indices[self.cat_dim] = torch.tensor(slice_indices)
            res = self._lazify(self.lazy_tensors[t_idx]._getitem(*indices))
            res_list.append(res)
            return self.__class__(*res_list, dim=new_cat_dim)

        res_list = []
        curr_tensor, slice_indices = target_tensors[0], []
        # group adjacent calls to the same LazyTensor
        for idx, t_idx in zip(target_indices, target_tensors):
            if t_idx != curr_tensor:
                indices[self.cat_dim] = torch.tensor(slice_indices)
                res = self._lazify(self.lazy_tensors[t_idx]._getitem(*indices))
                res_list.append(res)
                curr_tensor, slice_indices = t_idx, []
            slice_indices.append(idx - self.tensor_idx_to_start_idx[t_idx])
        indices[self.cat_dim] = torch.tensor(slice_indices)
        res = self._lazify(self.lazy_tensors[t_idx]._getitem(*indices))
        res_list.append(res)

        return self.__class__(*res_list, dim=self.cat_dim)

    def _get_indices(self, left_indices, right_indices, *batch_indices):
        # tensor indices must all have the same length
        indices = list(batch_indices) + [left_indices, right_indices]
        indices = torch.stack(indices, dim=0)
        target_indices = indices[self.cat_dim, :]
        target_tensors = [self.idx_to_tensor_idx[idx.item()] for idx in target_indices]
        starting_indices = [self.tensor_idx_to_start_idx[t_idx] for t_idx in target_tensors]
        local_indices = target_indices - torch.tensor(starting_indices)
        indices[self.cat_dim, :] = local_indices
        if len(set(target_tensors)) == 1:
            # shortcut if target_indices are all on the same LazyTensor
            left_indices, right_indices = indices[-2, :], indices[-1, :]
            batch_indices = tuple(indices[:-2, :])
            return self.lazy_tensors[target_tensors[0]]._get_indices(left_indices, right_indices, *batch_indices)

        d = defaultdict(list)
        for i, t_idx in enumerate(target_tensors):
            d[t_idx].append(i)

        res_list = []
        for t_idx, slices in sorted(d.items()):
            indices_ = indices[:, slices]
            left_indices, right_indices = indices_[-2, :], indices_[-1, :]
            batch_indices = tuple(indices_[:-2, :])
            res = self.lazy_tensors[t_idx]._get_indices(left_indices,
                                                        right_indices,
                                                        *batch_indices)
            res_list.append(res)
        # collect all the res in res_list onto one device
        res = torch.cat([r.to(self.device) for r in res_list], dim=0)

        t_idx_to_res_idx = []
        curr_idx = 0
        for t_idx in sorted(d.keys()):
            t_idx_to_res_idx.append(curr_idx)
            curr_idx += len(d[t_idx])
        lookup = []
        # use the fact that order of elements retrieved from each LazyTensor is
        # the same as the order they appear in target_indices
        for t_idx in target_tensors:
            idx = t_idx_to_res_idx[t_idx]
            lookup.append(idx)
            t_idx_to_res_idx[t_idx] += 1
        #indices_ = [slice(None, None, None)] * len(indices)
        #indices_[self.cat_dim] = lookup
        #return res[indices_]
        return res[lookup]

    def _matmul(self, rhs):
        isvector = rhs.ndimension() == 1
        if isvector:
            rhs = rhs.unsqueeze(1)

        output_device = (self.device if self.device is not None
                         else rhs.device)
        # make a copy of `rhs` on each device
        rhs_ = []
        for d in self.devices:
            if d != rhs.device:
                rhs_.append(rhs.to(d))
            else:
                rhs_.append(rhs)

        if self.cat_dim == self.ndimension() - 2:
            res_list = [t._matmul(rhs)
                        for t, rhs in zip(self.lazy_tensors, rhs_)]
            # copy result back to output device
            res_list = [x.to(output_device) for x in res_list]
            res = torch.cat(res_list, dim=self.cat_dim)
        elif self.cat_dim == self.ndimension() - 1:
            curr_idx = 0
            res_list = []
            index = [slice(None, None, None) for _ in range(rhs.ndimension())]
            for t, size, rhs in zip(self.lazy_tensors, self.cat_dim_sizes, rhs_):
                index[-2] = slice(curr_idx, curr_idx + size, None)
                res_list.append(t._matmul(rhs[index]))
                curr_idx += size
            # copy result back to output device
            res_list = [x.to(output_device) for x in res_list]
            res = torch.sum(torch.stack(res_list), dim=0)
        else:
            while rhs.ndimension() < self.ndimension():
                rhs = rhs.unsqueeze(0)
            curr_idx = 0
            res_list = []
            index = [slice(None, None, None) for _ in range(self.ndimension())]
            for t, size, rhs in zip(self.lazy_tensors, self.cat_dim_sizes, rhs_):
                index[self.cat_dim] = slice(curr_idx, curr_idx + size, None)
                res_list.append(t._matmul(rhs[index]))
                curr_idx += size
            # copy result back to output device
            res_list = [x.to(output_device) for x in res_list]
            res = torch.cat(res_list, dim=self.cat_dim)

        if isvector:
            res = res.squeeze(-1)
        return res

    def _size(self):
        size = self.pre_cat_size + (self.cat_dim_len,) + self.post_cat_size
        return torch.Size(size)

    def _transpose_nonbatch(self):
        if self.cat_dim == self.ndimension() - 2:
            new_dim = self.cat_dim + 1
        elif self.cat_dim == self.ndimension() - 1:
            new_dim = self.cat_dim - 1
        else:
            new_dim = self.cat_dim
        return self.__class__(*[t._transpose_nonbatch()
                                for t in self.lazy_tensors], dim=new_dim)

    def diag(self):
        return super().diag().to(self.device)

    def __getitem__(self, *indices):
        res = super().__getitem__(*indices)
        if not isinstance(res, CatLazyTensor):
            res = res.to(self.device)

        return res

    def inv_matmul(self, tensor):
        return super().inv_matmul(tensor).to(self.device)

    def inv_quad(self, tensor):
        return super().inv_quad(tensor).to(self.device)

    def inv_quad_log_det(self, inv_quad_rhs=None, log_det=False, reduce_inv_quad=True):
        res = super().inv_quad_log_det(self, inv_quad_rhs, log_det, reduce_inv_quad)
        return res.to(self.device)

    def matmul(self, other):
        return super().matmul(other).to(self.device)

    @property
    def device(self):
        return self.output_device

    @property
    def devices(self):
        return [t.device for t in self.lazy_tensors]

    @property
    def device_count(self):
        return len(set(self.devices))

    def _lazify(self, tsr):
        if torch.is_tensor(tsr):
            tsr = NonLazyTensor(tsr)
        elif not isinstance(tsr, LazyTensor):
            raise RuntimeError("Can only _lazify a Tensor or a LazyTensor")
        return tsr