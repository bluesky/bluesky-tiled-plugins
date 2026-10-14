from fastapi import APIRouter
from tiled.server.router import *
from tiled.server.dependencies import get_entry, get_root_tree
from tiled.server.authentication import check_scopes
from fastapi import HTTPException
import numpy

from typing import Any, Optional, List
from collections.abc import Callable
from fastapi import Request, Depends, Query, Security
from scanspec import specs
from scanspec.core import stack2dimension

router = APIRouter()


@router.get("/folded/{path:path}")
async def folded(
    path: str,
    request: Request,
    slice=Depends(NDSlice.from_query),
    expected_shape=Depends(expected_shape),
    format: Optional[str] = None,
    filename: Optional[str] = None,
    settings: Settings = Depends(get_settings),
    principal: Optional[Principal] = Depends(get_current_principal),
    root_tree=Depends(get_root_tree),
    session_state: dict = Depends(get_session_state),
    authn_access_tags: Optional[AccessTags] = Depends(get_current_access_tags),
    authn_scopes: Scopes = Depends(get_current_scopes),
    _=Security(check_scopes, scopes=["read:data"]),
):
    """Fetch a folded representation of an array dataset."""
    entry = await get_entry(
        path,
        ["read:data"],
        principal,
        authn_access_tags,
        authn_scopes,
        root_tree,
        session_state,
        request.state.metrics,
        None,
        getattr(request.app.state, "access_policy", None),
    )
    structure_family = entry.structure_family

    try:
        with record_timing(request.state.metrics, "read"):
            data = await ensure_awaitable(entry.read)
        if structure_family == StructureFamily.array:
            data = numpy.asarray(data)
    except Exception as e:
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error reading array data from entry at path '{path}': {e}",
        ) from e
    
    metadata = entry.metadata()

    if (spec := metadata.get("scanspec")) is None:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="No scanspec found in metadata; cannot fold data.",
        )
    
    spec = specs.Spec.deserialize(spec)

    def validate_spec(spec: specs.Spec[Any], validator: Callable[[specs.Spec[Any]], bool]) -> bool:
        """Recursively validate a Spec object using a custom validator."""

        is_valid = validator(spec)

        if not is_valid:
            return False

        # Recursively check nested specs
        for _, value in spec.__dict__.items():
            if isinstance(value, Spec):
                if not validate_spec(value, validator):
                    return False

        return True
    
    def resolve_simple_grid(spec: specs.Spec[Any], data: numpy.ndarray) -> numpy.ndarray:
        dim = stack2dimension(spec.calculate())
        midpoints = dim.midpoints
        shape = [len(set(midpoints[axis])) for axis in dim.axes()]
        idx = numpy.lexsort(tuple(midpoints[axis] for axis in dim.axes()))
        return idx.reshape(shape)
    
    if validate_spec(spec, lambda x: isinstance(
        x, (
            specs.Product,
            specs.Snake, 
            specs.Squash,
            specs.Linspace,
            specs.Range,
            specs.Static,
            specs.Fly
        )
    )):
        # Full regular grid
        idx = resolve_simple_grid(spec, data)
        folded_array = data[idx[slice]]
        return {"data": folded_array.tolist()}  # array_data

    else:
        raise ValueError(f"{path} does not contain a foldable scanspec.")


@router.get("/binned/{path:path}")
async def binned(
    path: str,
    request: Request,
    x: int,
    y: int,
    xmin: Optional[float] = None,
    xmax: Optional[float] = None,
    ymin: Optional[float] = None,
    ymax: Optional[float] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    slice_dim: Optional[List[str]] = Query(
        None,
        description="Repeatable: dim:center:thickness"
    ),
    principal: Optional[Principal] = Depends(get_current_principal),
    root_tree=Depends(get_root_tree),
    session_state: dict = Depends(get_session_state),
    authn_access_tags: Optional[AccessTags] = Depends(get_current_access_tags),
    authn_scopes: Scopes = Depends(get_current_scopes),
    _=Security(check_scopes, scopes=["read:data"]),
):
    """Fetch a folded representation of an array dataset."""
    entry = await get_entry(
        path,
        ["read:data"],
        principal,
        authn_access_tags,
        authn_scopes,
        root_tree,
        session_state,
        request.state.metrics,
        None,
        getattr(request.app.state, "access_policy", None),
    )

    try:
        with record_timing(request.state.metrics, "read"):
            data = await ensure_awaitable(entry.read)
    except Exception as e:
        raise HTTPException(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error reading array data from entry at path '{path}': {e}",
        ) from e

    readbacks = numpy.array([
        data['sample_stage-x'],
        data['sample_stage-y'],
        data['sample_stage-z'],
    ])

    # mask out the points that lie outside the slice
    mask = numpy.ones(data.size, dtype=bool)
    for entry in slice_dim:
        try:
            dim_str, center_str, thick_str = entry.split(":")
            dim = int(dim_str)
            center = float(center_str)
            thickness = float(thick_str)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid slice_dim format: {entry}. Expected dim:center:thickness"
            )
        if dim in (x, y):
            raise HTTPException(
                status_code=400,
                detail=f"slice_dim cannot contain x or y dimension {dim}"
            )
        mask &= numpy.abs(readbacks[dim, :] - center) <= thickness

    readbacks = readbacks[:, mask]
    data = data[mask]

    readback_x = readbacks[x, :]
    readback_y = readbacks[y, :]

    # bundle the kwargs
    histogram2d_kwargs = {}
    if all(opt is not None for opt in (width, height)):
        histogram2d_kwargs["bins"] = (width, height)
    if all(opt is not None for opt in (xmin, xmax, ymin, ymax)):
        histogram2d_kwargs["range"] = ((xmin, xmax), (ymin, ymax))

    binned_output = {
        channel: compute_binned_image(data[channel], readback_x, readback_y, **histogram2d_kwargs)
        for channel in ("RedTotal", "GreenTotal", "BlueTotal")
    }

    return {"data": binned_output}


def compute_binned_image(data, readback_x, readback_y, **kwargs):
    counts, edges_x, edges_y = numpy.histogram2d(readback_x, readback_y)
    weights, _, _ = numpy.histogram2d(readback_x, readback_y, weights=data, **kwargs)
    img = numpy.divide(weights, counts, out=numpy.zeros_like(weights), where=counts>0)
    return {"img": img, "x": edges_x, "y": edges_y}
