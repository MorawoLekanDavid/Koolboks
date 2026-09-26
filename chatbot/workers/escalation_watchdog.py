import asyncio

from chatbot.config import ESCALATION_SLA_HOURS, log
from chatbot.services.escalation_service import check_sla_breaches


async def escalation_watchdog_worker():
    """Periodic background task pairing handoff_watchdog_worker's pattern --
    catches an escalation whose owner has gone quiet for ESCALATION_SLA_HOURS
    with no first response, and pings the routing fallback agent as a
    backstop (see check_sla_breaches). Runs on the same 30-minute cadence as
    the handoff watchdog; there's no reason for this one to poll faster."""
    log.info(f"Escalation SLA watchdog started (sla_hours={ESCALATION_SLA_HOURS})")
    while True:
        try:
            await asyncio.sleep(1800)
            sent = await check_sla_breaches()
            if sent:
                log.info(f"[escalation-watchdog] sent SLA-breach fallback alert for {sent} escalation(s)")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Escalation watchdog error: {e}")
