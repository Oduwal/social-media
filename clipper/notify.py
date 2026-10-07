import logging

log = logging.getLogger(__name__)


def toast(title: str, body: str, url: str = None) -> None:
    """Windows notification; never lets a notification problem break the pipeline."""
    try:
        from win11toast import notify
        notify(title, body, on_click=url) if url else notify(title, body)
    except Exception as err:  # noqa: BLE001
        log.debug("Toast failed: %s", err)
