import einx, inspect
from . import util
import numpy as np

@einx.lru_cache(trace=lambda k: k[0] in [1, "tensors_in"])
def vmap_stage3(exprs_in, tensors_in, exprs_out, backend=None, op=None, verbose=False):
    if backend is None:
        backend = einx.backend.get(tensors_in)
    if op is None:
        raise TypeError("op cannot be None")
    if isinstance(op, str):
        op = getattr(backend, op)
    if len(exprs_in) != len(tensors_in):
        raise ValueError(f"Expected {len(exprs_in)} input tensor(s), got {len(tensors_in)}")

    # Call tensor factories
    tensors_in = [einx.param.instantiate(tensor, expr.shape, backend) for tensor, expr in zip(tensors_in, exprs_in)]

    if verbose:
        print("Expressions:")
        print("    IN:", [str(e) for e in exprs_in])
        print("    OUT:", [str(e) for e in exprs_out])

    # Flatten expressions
    exprs_in_flat, tensors_in = util.flatten(exprs_in, tensors_in, backend)
    exprs_out_flat = util.flatten(exprs_out)
    assert all(einx.expr.stage3.is_flat(expr) for expr in exprs_in_flat)
    assert all(einx.expr.stage3.is_flat(expr) for expr in exprs_out_flat)

    if verbose:
        print("Flat expressions:")
        print("    IN:", [str(e) for e in exprs_in_flat])
        print("    OUT:", [str(e) for e in exprs_out_flat])

    # In op: Unflatten input arguments, flatten output arguments
    exprs_in_funcargs = [einx.expr.stage3.get_marked(expr) for expr in exprs_in]
    exprs_out_funcargs = [einx.expr.stage3.get_marked(expr) for expr in exprs_out]
    exprs_in_funcargs_flat = [einx.expr.stage3.get_marked(expr) for expr in exprs_in_flat]
    exprs_out_funcargs_flat = [einx.expr.stage3.get_marked(expr) for expr in exprs_out_flat]

    if verbose:
        print("Expressions used in op:")
        print("    IN:", [str(e) for e in exprs_in_funcargs])
        print("    OUT:", [str(e) for e in exprs_out_funcargs])
        print("    IN_FLAT:", [str(e) for e in exprs_in_funcargs_flat])
        print("    OUT_FLAT:", [str(e) for e in exprs_out_funcargs_flat])

    def op(*tensors_in_flat, op=op):
        if verbose:
            print("Flat input tensors that arrived in op:", [str(a.shape) for a in tensors_in_flat])
            print("Input types to vmapped function:", [type(t) for t in tensors_in_flat])
        assert len(tensors_in_flat) == len(exprs_in_funcargs_flat)

        tensors_in = util.unflatten(exprs_in_funcargs_flat, tensors_in_flat, exprs_in_funcargs, backend=None)
        if verbose:
            print("Unflattened input tensors in op:", [str(a.shape) for a in tensors_in])
        assert len(tensors_in) == len(exprs_in)

        tensors_out = op(*tensors_in)
        if not isinstance(tensors_out, (tuple, list)):
            tensors_out = (tensors_out,)
        if len(tensors_out) != len(exprs_out_funcargs):
            raise ValueError(f"Expected {len(exprs_out_funcargs)} output tensor(s) from vmapped function, but got {len(tensors_out)}")

        if verbose:
            print("Unflattened output tensors in op:")
            for i, (expr_out, tensor_out) in enumerate(zip(exprs_out_funcargs, tensors_out)):
                print("    ", expr_out, tensor_out.shape)

        for i, (expr_out, tensor_out) in enumerate(zip(exprs_out_funcargs, tensors_out)):
            if tensor_out.shape != expr_out.shape:
                raise ValueError(f"Expected output shape {expr_out.shape} from {i}-th (zero-based) output of vmapped function, but got {tensor_out.shape}")

        exprs_out_funcargs_flat2, tensors_out = util.flatten(exprs_out_funcargs, tensors_out, backend=None)

        if verbose:
            print("Flattened output tensors in op:", [str(a.shape) for a in tensors_out])
        assert exprs_out_funcargs_flat2 == exprs_out_funcargs_flat, f"{[str(s) for s in exprs_out_funcargs_flat2]} != {[str(s) for s in exprs_out_funcargs_flat]}"

        if verbose:
            print("Returning types from vmapped function:", [type(t) for t in tensors_out])
        return tuple(tensors_out)

    # Get ordered list of vmapped axes
    def is_vmapped(expr):
        return not einx.expr.stage3.is_marked(expr)
    vmapped_axes = []
    for root in list(exprs_in_flat):
        for v in root:
            if is_vmapped(v) and not v.name in vmapped_axes:
                vmapped_axes.append(v.name)
    if len(vmapped_axes) == 0:
        raise ValueError("No vmapped axes found")
    if verbose:
        print(f"Vmapping the following axes: {vmapped_axes}")
    for root in list(exprs_in_flat) + list(exprs_out_flat):
        for v in root:
            if (v.name in vmapped_axes) != is_vmapped(v):
                raise ValueError(f"Axis {v.name} appears both as vmapped and non-vmapped")

    # Apply vmap to op
    axes_names_in = [[a.name for a in root] for root in exprs_in_flat]
    axes_names_in_set = set(a.name for root in exprs_in_flat for a in root)
    is_broadcast_axis = lambda expr: isinstance(expr, einx.expr.stage3.Axis) and not expr.name in axes_names_in_set and not einx.expr.stage3.is_marked(expr)
    exprs_out_flat_without_broadcast = [einx.expr.stage3.remove(expr, is_broadcast_axis) for expr in exprs_out_flat]
    axes_names_out_without_broadcast = [[a.name for a in root] for root in exprs_out_flat_without_broadcast]

    axisname_to_value = {a.name: a.value for root in exprs_out_flat_without_broadcast for a in root}

    if verbose:
        print("Flat output expressions without broadcast:", [str(e) for e in exprs_out_flat_without_broadcast])
        print("Got input axis names:", axes_names_in)
        print("Got output axis names (excluding broadcasted output axes):", axes_names_out_without_broadcast)

    vmaps = []
    for v in vmapped_axes: # TODO: best order for vmapped_axes?
        in_axes = tuple(axes_names.index(v) if v in axes_names else None for axes_names in axes_names_in)
        out_axes = tuple(axes_names.index(v) if v in axes_names else None for axes_names in axes_names_out_without_broadcast)
        if verbose:
            print(f"Applying backend.vmap to axis {v}, with input axis indices {in_axes} and output axis indices {out_axes}")
        for out_axis, expr_out in zip(out_axes, exprs_out_flat):
            if out_axis is None:
                raise ValueError(f"All vmapped axes must appear in the output expression, but '{v}' does not appear in '{expr_out}'") # TODO: test

        out_shapes = [tuple(axisname_to_value[n] for n in axislist) for axislist in axes_names_out_without_broadcast]
        vmaps.append((in_axes, out_axes, out_shapes))

        for axes_names in axes_names_in + axes_names_out_without_broadcast:
            if v in axes_names:
                axes_names.remove(v)
            if v in axes_names_out_without_broadcast:
                axes_names_out_without_broadcast.remove(v)
        if verbose:
            print(f"Now has remaining input axes {axes_names_in} and output axes {axes_names_out_without_broadcast}")

    for in_axes, out_axes, out_shapes in reversed(vmaps):
        op = backend.vmap(op, in_axes=in_axes, out_axes=out_axes)

    # Apply op to tensors
    if verbose:
        print("\nSending shapes to backend.vmap:", [str(a.shape) for a in tensors_in])
    tensors = op(*tensors_in)
    tensors = [backend.assert_shape(tensor, expr.shape) for tensor, expr in zip(tensors, exprs_out_flat_without_broadcast)]

    if verbose:
        for tensor, expr in zip(tensors, exprs_out_flat_without_broadcast):
            print("Got overall flat tensor_out:", tensor.shape, expr)

    # Transpose and broadcast missing output dimensions
    tensors = [util.transpose_broadcast(expr_out_wb, tensor, expr_out) for expr_out_wb, tensor, expr_out in zip(exprs_out_flat_without_broadcast, tensors, exprs_out_flat)]
    if verbose:
        print("Got overall transposed+broadcasted tensors_out:")
        for tensor, expr in zip(tensors, exprs_out_flat):
            print("    ", tensor.shape, expr)

    # Unflatten output expressions
    tensors = util.unflatten(exprs_out_flat, tensors, exprs_out, backend)
    if verbose:
        print("Got overall unflattened tensors_out:", [str(a.shape) for a in tensors])

    return tensors, exprs_out

def parse(description, *tensor_shapes, cse=True, **parameters):
    if isinstance(description, tuple):
        if len(description) != 2:
            raise ValueError("Expected tuple of length 2")
        for k in parameters:
            if k in description[1]:
                raise ValueError(f"Parameter '{k}' is given twice")
        parameters.update(description[1])
        description = description[0]
    if not isinstance(description, str):
        raise ValueError("First argument must be an operation string")

    description = description.split("->")

    if len(description) != 2:
        raise ValueError("Operation string must contain exactly one '->'")
    exprs_in, exprs_out = description
    exprs_in = exprs_in.split(",")
    exprs_out = exprs_out.split(",")

    if len(exprs_in) != len(tensor_shapes):
        raise ValueError(f"Expected {len(exprs_in)} input tensor(s), got {len(tensor_shapes)}")

    exprs = einx.expr.solve(
           [einx.expr.Condition(expr=expr_in, value=tensor_shape, depth=0) for expr_in, tensor_shape in zip(exprs_in, tensor_shapes)] \
         + [einx.expr.Condition(expr=expr_out, depth=0) for expr_out in exprs_out] \
         + [einx.expr.Condition(expr=k, value=np.asarray(v)[..., np.newaxis]) for k, v in parameters.items()],
        cse=cse,
        cse_concat=False,
    )[:len(exprs_in) + len(exprs_out)]
    exprs_in, exprs_out = exprs[:len(exprs_in)], exprs[len(exprs_in):]

    return exprs_in, exprs_out

@einx.lru_cache(trace=lambda k: isinstance(k[0], int) and k[0] >= 1)
def vmap_stage0(description, *tensors, op, backend=None, cse=True, **parameters):
    exprs_in, exprs_out = parse(description, *[einx.param.get_shape(tensor) for tensor in tensors], cse=cse, **parameters)
    tensors, exprs_out = vmap_stage3(exprs_in, tensors, exprs_out, backend=backend, op=op)
    return tensors[0] if len(exprs_out) == 1 else tensors

def vmap(arg0, *args, **kwargs):
    if isinstance(arg0, str) or (isinstance(arg0, tuple) and isinstance(arg0[0], str)):
        return vmap_stage0(arg0, *args, **kwargs)
    else:
        return vmap_stage3(arg0, *args, **kwargs)