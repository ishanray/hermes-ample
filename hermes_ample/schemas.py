"""Tool schemas: what the model reads to decide when and how to call each tool."""

DEPLOY = {
    "name": "ample_deploy",
    "description": (
        "Put a website or web app online at a public HTTPS URL with Ample, or release changes to one already online. "
        "Deploys a folder on this machine: a static site, a Node, Python, Go, Ruby or PHP app, or a project of several "
        "services (for example web/ and api/ folders). Ample works out what the folder holds (services, databases, "
        "start commands), builds it, and waits until it is live or failed, which can take a few minutes; call it once "
        "and read the result, never again to check on it. Running it again on the same folder releases changes to the "
        "same URLs; with nothing changed it returns at once. If Ample has open questions it returns them: answer with "
        "`answers` and call again. On failure, read the diagnosis and suggested fix, change the code, and deploy again. "
        "Everything in the folder is uploaded and the URL is public, so deploy only the app's own folder and only when "
        "the user asked to publish it. Load the skill `ample:web-apps` for how to build sites that deploy first time."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The app's own folder, absolute or relative to the working directory, for example ./bake-sale. Never a home directory or a folder of unrelated files.",
            },
            "name": {
                "type": "string",
                "description": "App name for a single app, lowercase letters, digits and hyphens; it becomes part of the URL. Default: the folder's name.",
            },
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Secrets the app reads from its environment, such as API keys the user gave you. Ample stores them encrypted; never put them in the code.",
            },
            "answers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": 'Answers to the plan\'s open questions, keyed by the path each question names, for example {"services.web.start": "node server.js"}.',
            },
            "force": {
                "type": "boolean",
                "description": "Rebuild and restart even when nothing changed. Only to recover an app that is live but misbehaving.",
            },
        },
        "required": ["path"],
    },
}

LOGS = {
    "name": "ample_logs",
    "description": (
        "Read an Ample app's build or runtime logs by deployment ID (dep_...). Use after a failed deploy, or when an "
        "app that is live misbehaves."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deployment_id": {"type": "string", "description": "The deployment ID from ample_deploy or ample_apps."},
            "kind": {"type": "string", "enum": ["build", "runtime", "all"], "description": "Which logs. Default all."},
            "tail": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Last N lines. Default 100."},
        },
        "required": ["deployment_id"],
    },
}

APPS = {
    "name": "ample_apps",
    "description": "List the apps deployed with Ample: name, deployment ID, status and URL.",
    "parameters": {"type": "object", "properties": {}},
}

DELETE = {
    "name": "ample_app_delete",
    "description": (
        "Take an Ample app offline and delete its releases; its URL stops working. Only when the user asked for this "
        "app to be removed. The user is asked to approve it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deployment_id": {"type": "string", "description": "The deployment ID (dep_...) from ample_apps."},
        },
        "required": ["deployment_id"],
    },
}
