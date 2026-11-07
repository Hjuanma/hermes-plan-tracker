"""JSON Schemas for plan-tracker tools."""
PLAN_TRACK_SCHEMA = {
    "name": "plan_track",
    "description": (
        "Manage registered markdown plan files in the Plan Tracker plugin."
        " Actions: add (register a path), remove (by id or path), list."
        " Scope: global or session (session requires a session id)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "remove", "list"],
                "description": "add: register a path; remove: unregister by id/path; list: show all",
            },
            "path": {
                "type": "string",
                "description": "Absolute path to a .md file (required for add; optional for remove)",
            },
            "id": {
                "type": "string",
                "description": "Plan id (12 hex chars) to remove (alternative to path)",
            },
            "scope": {
                "type": "string",
                "enum": ["global", "session"],
                "description": "Registration scope (default: global; session when session_id injected)",
            },
            "session": {
                "type": "string",
                "description": "Session id for session-scoped entries",
            },
        },
        "required": ["action"],
    },
}