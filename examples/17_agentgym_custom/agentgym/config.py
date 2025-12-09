"""AgentGym environment configuration resolver"""


def resolve_buildargs(buildargs: dict) -> dict:
    """
    Resolve build arguments for AgentGym environments

    This function takes user-provided buildargs and resolves them to complete
    Docker build arguments including BASE_IMAGE, ENV_NAME, and TOOL_NAME.

    Args:
        buildargs: User-provided build arguments (must contain "ENV_NAME")

    Returns:
        Resolved build arguments dictionary

    Raises:
        ValueError: If ENV_NAME is not provided or not supported
    """
    if "ENV_NAME" not in buildargs:
        raise ValueError("ENV_NAME must be provided in buildargs")

    env_name = buildargs["ENV_NAME"]

    allowed_envs = [
        "webshop",
        "alfworld",
        "babyai",
        "sciworld",
        "textcraft",
        "sqlgym",
        "maze",
        "wordle",
        "academia",
        "movie",
        "sheet",
        "todo",
        "weather",
    ]

    if env_name not in allowed_envs:
        raise ValueError(
            f"Invalid AgentGym environment name: '{env_name}'. "
            f"Allowed values are: {', '.join(allowed_envs)}"
        )

    # Default configuration
    base_image = "python:3.11-slim"
    tool_name = ""
    preinstall_env = env_name

    # Python 3.8 environments
    if env_name in ["webshop", "sciworld"]:
        base_image = "python:3.8-slim"

    # Tool environments (academia, movie, sheet, todo, weather)
    elif env_name in ["academia", "movie", "sheet", "todo", "weather"]:
        base_image = "python:3.8.13-slim"
        tool_name = env_name
        preinstall_env = "tool"

    # LMRLGym environments (maze, wordle)
    elif env_name in ["maze", "wordle"]:
        base_image = "python:3.9.12-slim"
        tool_name = env_name
        preinstall_env = "lmrlgym"

    return {
        "BASE_IMAGE": base_image,
        "ENV_NAME": preinstall_env,
        "TOOL_NAME": tool_name,
    }
