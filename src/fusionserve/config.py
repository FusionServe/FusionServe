from dynaconf import Dynaconf, Validator

settings = Dynaconf(
    envvar_prefix=False,
    settings_files=["settings.yaml", ".secrets.yaml"],
    environments=True,
    validators=[
        # Validator("rabbit_host", default="rabbitmq"),
        # Validator("pg_host", default="tsportal-pg"),
        Validator("log_level", default="INFO"),
    ],
)
