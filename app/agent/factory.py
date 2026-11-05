"""Choix du fournisseur de modèle selon la configuration du serveur (jamais selon le client)."""
import logging

from app.agent.llm import AnthropicLLM, LLMClient
from app.agent.llm_groq import GroqLLM

log = logging.getLogger(__name__)


def build_llm(settings) -> LLMClient:
    provider = settings.llm_provider
    # Garde-fou : un fournisseur de développement ne doit jamais servir de vrais clients.
    if provider != "anthropic" and settings.app_env == "production":
        raise SystemExit(
            f"LLM_PROVIDER={provider} est interdit quand APP_ENV=production : seuls les modèles Anthropic sont autorisés."
        )
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise SystemExit("ANTHROPIC_API_KEY manquant")
        return AnthropicLLM(settings.anthropic_api_key)
    if provider == "groq":
        if not settings.groq_api_key:
            raise SystemExit("GROQ_API_KEY manquant")
        log.warning("Fournisseur LLM = groq : développement uniquement, ne pas y envoyer de vraies données clients.")
        return GroqLLM(settings.groq_api_key, base_url=settings.groq_base_url)
    raise SystemExit(f"LLM_PROVIDER inconnu : {provider}")
