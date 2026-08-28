from app.integrations.olt.adapters.null import NullOltAdapter
from app.integrations.olt.contracts import OltAdapter


adapter_registry: dict[str, type[NullOltAdapter]] = {"null": NullOltAdapter}


def get_adapter(adapter_key: str, **kwargs) -> OltAdapter:
    adapter_type = adapter_registry.get(adapter_key)
    if adapter_type is None:
        raise ValueError("unsupported_adapter")
    return adapter_type(**kwargs)
