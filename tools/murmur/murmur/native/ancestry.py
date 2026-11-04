"""Bounded verified parent edges, independent of account enrollment."""
from .types import IdentityError, NativeError, validate_session_id


MAX_EDGES = 128


def extend(provider, session, parent, read_parent):
    """Keep a verified direct edge even when a later ancestor is unavailable.

    The callback must read exact native metadata under the caller's shared
    deadline. A provider-confirmed root ends the chain; an unavailable ancestor
    record never does. Each provider retains its qualified root semantics.
    """
    edges, seen = [], set()
    while True:
        validate_session_id(provider, session)
        if session in seen or parent in seen or session == parent:
            raise IdentityError("Native ancestry contains a cycle")
        seen.add(session)
        if parent is not None:
            validate_session_id(provider, parent)
        edges.append({"session_id": session, "parent_session_id": parent})
        if parent is None:
            return {"lineage_edges": edges, "lineage_complete": True}
        if len(edges) >= MAX_EDGES:
            return {"lineage_edges": edges, "lineage_complete": False,
                    "lineage_reason": "Native ancestry exceeds its bounded metadata coverage"}
        session = parent
        try:
            parent = read_parent(session)
        except (NativeError, OSError, RuntimeError) as error:
            return {"lineage_edges": edges, "lineage_complete": False,
                    "lineage_reason": "Ancestor metadata is unresolved: " + str(error)[:1000]}
