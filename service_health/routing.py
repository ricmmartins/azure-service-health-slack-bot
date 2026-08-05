from dataclasses import dataclass

from service_health.models import ServiceHealthEvent


class InvalidRoutingConfiguration(ValueError):
    pass


def _normalized_values(value, field_name):
    if value is None:
        return frozenset()
    if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value):
        raise InvalidRoutingConfiguration(
            f"'{field_name}' must be a list of non-empty strings")
    return frozenset(item.strip().casefold() for item in value)


@dataclass(frozen=True)
class RoutingRule:
    channel_id: str
    subscriptions: frozenset[str]
    services: frozenset[str]
    regions: frozenset[str]
    priority: int
    order: int

    @property
    def specificity(self):
        return sum(bool(value) for value in (
            self.subscriptions, self.services, self.regions))

    def matches(self, event: ServiceHealthEvent):
        return (
            (not self.subscriptions
             or event.subscription_id.casefold() in self.subscriptions)
            and (not self.services
                 or bool(event.service_names & self.services))
            and (not self.regions
                 or bool(event.region_names & self.regions))
        )


@dataclass(frozen=True)
class RoutingConfig:
    default_channel_id: str
    rules: tuple[RoutingRule, ...]

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise InvalidRoutingConfiguration(
                "Routing configuration must be an object")
        default_channel_id = value.get("default_channel_id")
        if not isinstance(default_channel_id, str) or not default_channel_id.strip():
            raise InvalidRoutingConfiguration(
                "'default_channel_id' is required")
        raw_rules = value.get("rules", [])
        if not isinstance(raw_rules, list):
            raise InvalidRoutingConfiguration("'rules' must be a list")

        rules = []
        for order, raw_rule in enumerate(raw_rules):
            if not isinstance(raw_rule, dict):
                raise InvalidRoutingConfiguration(
                    f"Rule {order} must be an object")
            channel_id = raw_rule.get("channel_id")
            priority = raw_rule.get("priority", 0)
            if not isinstance(channel_id, str) or not channel_id.strip():
                raise InvalidRoutingConfiguration(
                    f"Rule {order} requires 'channel_id'")
            if not isinstance(priority, int) or isinstance(priority, bool):
                raise InvalidRoutingConfiguration(
                    f"Rule {order} priority must be an integer")
            rules.append(RoutingRule(
                channel_id=channel_id.strip(),
                subscriptions=_normalized_values(
                    raw_rule.get("subscription_ids"), "subscription_ids"),
                services=_normalized_values(
                    raw_rule.get("services"), "services"),
                regions=_normalized_values(
                    raw_rule.get("regions"), "regions"),
                priority=priority,
                order=order,
            ))
        return cls(default_channel_id.strip(), tuple(rules))

    def channel_for(self, event: ServiceHealthEvent):
        matching = [rule for rule in self.rules if rule.matches(event)]
        if not matching:
            return self.default_channel_id
        selected = min(
            matching,
            key=lambda rule: (-rule.priority, -rule.specificity, rule.order),
        )
        return selected.channel_id
