"""Système d'outils.

Le modèle PROPOSE un outil et ses paramètres ; le code VALIDE, vérifie les droits et EXÉCUTE.
Le tenant_id n'est jamais un paramètre d'outil : il vient du contexte serveur (ToolContext).
"""
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.db import tenant_session
from app.models import ToolCall
from app.services.handoff import create_escalation

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    tenant_id: UUID
    conversation_id: UUID
    run_id: UUID
    config: dict  # configuration du client (tenants.config)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")  # un paramètre inattendu (ex. tenant_id) est refusé


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[StrictInput]
    handler: Callable[[Session, ToolContext, StrictInput], dict]

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


@dataclass
class ToolResult:
    ok: bool
    output: dict


# --- get_business_info ------------------------------------------------------------------

class BusinessInfoInput(StrictInput):
    section: str | None = Field(
        default=None, description="Rubrique souhaitée (ex. 'horaires', 'services'). Vide = toutes les rubriques."
    )


def _get_business_info(_: Session, ctx: ToolContext, args: BusinessInfoInput) -> dict:
    info = ctx.config.get("business_info")
    if not info:
        return {"available": False, "message": "Aucune information officielle n'est enregistrée pour cette entreprise."}
    if args.section:
        if args.section in info:
            return {"available": True, args.section: info[args.section]}
        return {"available": False, "message": f"Rubrique inconnue. Rubriques disponibles : {sorted(info)}"}
    return {"available": True, "business_info": info, "rubriques": sorted(info)}


# --- escalate_to_human ------------------------------------------------------------------

class EscalateInput(StrictInput):
    category: Literal["plainte", "litige", "paiement", "information_manquante", "sensible", "autre"] = Field(
        default="autre", description="Type de situation (information_manquante : tarif, délai ou pièce non renseignés)."
    )
    reason: str = Field(description="Pourquoi tu transmets, en une phrase (ce que l'équipe doit savoir sur ta décision).")
    summary: str = Field(description="Ce que demande le client, en 1 à 3 phrases, pour l'équipe.")


def _escalate(s: Session, ctx: ToolContext, args: EscalateInput) -> dict:
    create_escalation(
        s,
        tenant_id=ctx.tenant_id,
        conversation_id=ctx.conversation_id,
        category=args.category,
        reason=args.reason,
        summary=args.summary,
        source="agent",
        run_id=ctx.run_id,
    )
    return {
        "escalated": True,
        "instruction": "Informe le client, en une ou deux phrases, qu'un membre de l'équipe va reprendre la conversation.",
    }


REGISTRY: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "get_business_info",
            "Informations OFFICIELLES de l'entreprise (services, horaires, contacts, déroulement des démarches). "
            "À appeler avant de répondre à toute question factuelle sur l'entreprise.",
            BusinessInfoInput,
            _get_business_info,
        ),
        Tool(
            "escalate_to_human",
            "Transfère la conversation à un humain de l'équipe. L'agent cesse ensuite de répondre. "
            "À utiliser pour une plainte, un litige, un paiement, un sujet sensible, une information absente "
            "ou une demande hors périmètre.",
            EscalateInput,
            _escalate,
        ),
    )
}


def enabled_tools(config: dict) -> list[Tool]:
    """Seuls les outils activés pour ce client sont exposés au modèle."""
    names = config.get("tools_enabled", [])
    return [REGISTRY[n] for n in names if n in REGISTRY]


def run_tool(ctx: ToolContext, name: str, raw_input: dict) -> ToolResult:
    """Valide -> contrôle des droits -> exécute -> journalise. Ne lève jamais : renvoie une erreur lisible."""
    started = time.monotonic()
    ok, output, error = True, {}, None

    allowed = {t.name for t in enabled_tools(ctx.config)}
    tool = REGISTRY.get(name)
    if tool is None or name not in allowed:  # contrôle des droits CÔTÉ SERVEUR
        ok, error = False, f"Outil '{name}' non disponible."
    else:
        try:
            args = tool.input_model.model_validate(raw_input)
            with tenant_session(ctx.tenant_id) as s:
                output = tool.handler(s, ctx, args)
        except ValidationError as e:
            ok, error = False, "Paramètres invalides : " + "; ".join(
                f"{'.'.join(map(str, err['loc'])) or 'entrée'}: {err['msg']}" for err in e.errors()
            )
        except Exception:
            log.exception("échec de l'outil %s", name)
            ok, error = False, "Erreur interne lors de l'exécution de l'outil."

    if not ok:
        output = {"error": error}
    with tenant_session(ctx.tenant_id) as s:  # journalisation de CHAQUE appel
        s.add(
            ToolCall(
                tenant_id=ctx.tenant_id,
                run_id=ctx.run_id,
                tool_name=name,
                input=raw_input,
                output=output,
                status="ok" if ok else "error",
                error=error,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
    return ToolResult(ok, output)
