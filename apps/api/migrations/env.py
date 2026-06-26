from logging.config import fileConfig

from alembic import context

from core.db import Base, engine

from models.user import User
from models.bot import Bot
from models.signal import Signal
from models.order import Order
from models.position import Position
from models.subscriber import Subscriber
from models.intelligence_event import IntelligenceEvent
from models.telegram_delivery import TelegramDelivery
from models.telegram_profile import TelegramProfile
from models.audit_event import AuditEvent
from models.payment import BillingPlan, Payment, PaymentEvent
from models.funding_arbitrage import FundingArbOpportunity, FundingArbPosition
from models.grid_state import GridState

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()