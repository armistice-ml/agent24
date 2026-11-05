from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str                 # rôle applicatif (soumis à la RLS)
    migrations_database_url: str      # rôle propriétaire (migrations, scripts d'admin)
    meta_app_secret: str              # secret de l'app Meta : vérifie la signature des webhooks
    messenger_verify_token: str       # jeton de vérification de l'abonnement webhook
    encryption_key: str               # clé Fernet
    log_level: str = "INFO"

    anthropic_api_key: str = ""       # requis pour le worker (sauf tests avec faux LLM)
    agent_model: str = "claude-sonnet-5-5"  # modèle par défaut, surchargeable par client (config.agent.model)
    graph_api_version: str = "v23.0"  # à vérifier dans le changelog Meta
    send_mode: Literal["messenger", "console"] = "messenger"  # "console" : affiche au lieu d'envoyer
    worker_poll_seconds: float = 1.0
    meta_app_id: str = ""             # identifiant de l'app Meta (détection des réponses humaines)

    # Tableau de bord
    dashboard_origin: str = ""        # ex. https://app.exemple.com : CORS + contrôle d'Origin (CSRF)
    dashboard_base_url: str = ""      # base des liens dans les emails
    cookie_secure: bool = True        # mettre false UNIQUEMENT en développement sans HTTPS
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_hours: int = 12           # durée de session glissante

    # Email (notification d'escalade au dirigeant)
    email_mode: Literal["console", "smtp"] = "console"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    email_from: str = ""
    
    app_env: Literal["development", "production"] = "production"
    llm_provider: Literal["anthropic", "groq"] = "anthropic"
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
