import re

from fastapi import APIRouter
from tiled.server.dependencies import get_entry, get_root_tree
from tiled.server.authentication import check_scopes
from fastapi import HTTPException
import pydantic
from ..writing.validator import validate_data_source, StructureValidationException

from typing import Optional
from fastapi import Request, Depends, Query, Security

from tiled.structures.core import Spec
from tiled.structures.data_source import Management
from tiled.type_aliases import AccessTags, Scopes
from tiled.server.authentication import (
    get_current_access_tags,
    get_current_principal,
    get_current_scopes,
    get_session_state,
)
from tiled.server.settings import Settings, get_settings
from tiled.server.schemas import Principal

from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_500_INTERNAL_SERVER_ERROR,
)


router = APIRouter()


class ValidationResponse(pydantic.BaseModel):
    valid: bool
    notes: list[str]


class PostValidationRequest(pydantic.BaseModel):
    ignore_errors: Optional[list[str]] = None


async def validate_entry_structure(
    entry, fix: bool, ignore_errors: Optional[list[str]] = None
) -> tuple[bool, list[str]]:
    """Validate the structure of data sources in the given entry.

    Parameters:
    ----------
    entry: Entry
        The entry whose data sources should be validated.
    fix: bool
        If True, attempt to correct any structural issues in the data sources.
    ignore_errors: list[str], optional
        A list of (parts of) error messages to ignore during validation. If an error message
        matches any in this list, it will not cause the validation to fail, and its details
        will not be included in the returned notes.

    Returns:
    -------
    valid: bool
        True if all data sources are valid (or were successfully fixed), False otherwise.
    notes: list[str]
        A list of notes detailing any issues found and/or corrections made during validation.
        If `valid` is False, this list will contain descriptions of the validation failures.
    """
    notes = []
    ignore_errors = ignore_errors or []
    for stream_name, stream_node in await entry.items_range(0, None):
        for dkey_name, dkey_node in await stream_node.items_range(0, None):
            for data_source in dkey_node.data_sources:
                if data_source.management == Management.external:
                    try:
                        valid_data_source, _notes = validate_data_source(
                            data_source,
                            fix_errors=fix,
                            metadata=dkey_node.metadata(),
                            adapters_by_mimetype=entry.context.adapters_by_mimetype,
                        )
                        notes.extend(
                            [
                                f"Structure validation of '{stream_name}/{dkey_name}': {note}"
                                for note in _notes
                            ]
                        )

                    except StructureValidationException as e:
                        msg = f"Structure validation of '{stream_name}/{dkey_name}' failed: {e}"
                        return False, [msg]

                    except Exception as e:
                        if any(re.search(msg, str(e)) for msg in ignore_errors):
                            notes.append(
                                f"Ignored error during validation of '{stream_name}/{dkey_name}': {e}"
                            )
                            continue

                        msg = f"Unexpected error during validation of '{stream_name}/{dkey_name}': {e}"
                        raise HTTPException(
                            status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=msg
                        )

                    # If the data source was modified during validation, update it on the server
                    if _notes:
                        await dkey_node.put_data_source(valid_data_source, patch=None)

    return True, notes


@router.get("/validate/{path:path}")
async def validate_structure_operation(
    path: str,
    request: Request,
    fix: bool = Query(False, description="Attempt to correct structure to match data."),
    settings: Settings = Depends(get_settings),
    principal: Optional[Principal] = Depends(get_current_principal),
    root_tree=Depends(get_root_tree),
    session_state: dict = Depends(get_session_state),
    authn_access_tags: Optional[AccessTags] = Depends(get_current_access_tags),
    authn_scopes: Scopes = Depends(get_current_scopes),
    _=Security(check_scopes, scopes=["read:data", "read:metadata", "write:metadata"]),
):
    entry = await get_entry(
        path,
        ["read:data", "read:metadata", "write:metadata"],
        principal,
        authn_access_tags,
        authn_scopes,
        root_tree,
        session_state,
        request.state.metrics,
        None,
        getattr(request.app.state, "access_policy", None),
    )

    if Spec("BlueskyRun", version="3.0") not in entry.specs:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Entry at path '{path}' does not have a BlueskyRun spec; cannot validate.",
        )

    valid, notes = await validate_entry_structure(entry, fix=fix)

    return ValidationResponse(valid=valid, notes=notes)


@router.post("/validate/{path:path}")
async def validate_structure_operation_post(
    path: str,
    body: PostValidationRequest,
    request: Request,
    fix: bool = Query(False, description="Attempt to correct structure to match data."),
    settings: Settings = Depends(get_settings),
    principal: Optional[Principal] = Depends(get_current_principal),
    root_tree=Depends(get_root_tree),
    session_state: dict = Depends(get_session_state),
    authn_access_tags: Optional[AccessTags] = Depends(get_current_access_tags),
    authn_scopes: Scopes = Depends(get_current_scopes),
    _=Security(check_scopes, scopes=["read:data", "read:metadata", "write:metadata"]),
):
    # POST version of the same endpoint, to allow for longer query parameters (e.g. ignore_errors)
    entry = await get_entry(
        path,
        ["read:data", "read:metadata", "write:metadata"],
        principal,
        authn_access_tags,
        authn_scopes,
        root_tree,
        session_state,
        request.state.metrics,
        None,
        getattr(request.app.state, "access_policy", None),
    )

    if Spec("BlueskyRun", version="3.0") not in entry.specs:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Entry at path '{path}' does not have a BlueskyRun spec; cannot validate.",
        )

    valid, notes = await validate_entry_structure(
        entry, fix=fix, ignore_errors=body.ignore_errors
    )

    return ValidationResponse(valid=valid, notes=notes)
