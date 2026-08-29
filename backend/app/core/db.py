"""MongoDB connection and first-run bootstrap."""
from __future__ import annotations

import logging

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from app.core.security import generate_password, hash_password
from app.models.entities import ALL_DOCUMENTS, Period, Plan, Role, User

logger = logging.getLogger("excelclear.db")
_client: AsyncIOMotorClient | None = None


async def connect() -> None:
    global _client
    settings = get_settings()
    _client = AsyncIOMotorClient(settings.mongo_url, serverSelectionTimeoutMS=5000)
    # Fail loudly at boot rather than on the first request a customer makes.
    await _client.admin.command("ping")
    await init_beanie(database=_client[settings.mongo_db], document_models=ALL_DOCUMENTS)
    logger.info("mongo connected db=%s", settings.mongo_db)
    await ensure_first_admin()
    await ensure_plans()


async def disconnect() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


async def ensure_first_admin() -> None:
    """Create one administrator if the database has none.

    The generated password is printed once, to the server log, and never stored
    in recoverable form. An install that silently shipped a known default
    password would be worse than no account at all.
    """
    if await User.find_one(User.role == Role.ADMIN) is not None:
        return
    password = generate_password()
    admin = User(
        email=get_settings().first_admin_email,
        password_hash=hash_password(password),
        role=Role.ADMIN,
        display_name="System administrator",
        monthly_quota=0,
    )
    await admin.insert()
    logger.warning(
        "\n%s\n  First-run administrator created\n    email    : %s\n    password : %s\n"
        "  Shown once. Sign in and change it.\n%s",
        "=" * 62, admin.email, password, "=" * 62,
    )


# Plain dicts, not Plan objects: a Beanie document cannot be built before
# init_beanie has run, and this module is imported long before that.
_STARTING_PLANS = [
    dict(
        slug="silver",
        name_ar="الباقة الفضية",
        name_en="Silver",
        price_amount=99,
        period=Period.MONTHLY,
        monthly_limit=3_000,
        # The monthly limit has its own line on the card, so it is not repeated
        # here — a feature list that restates the headline reads as padding.
        features_ar=[
            "تحويل الفواتير المصوّرة والورقية إلى Excel",
            "تنظيف ملفات Excel — غير محدود",
            "البوابات الثلاث للتحقق على كل مستند",
            "دعم عبر البريد",
        ],
        features_en=[
            "Photographed and paper invoices to Excel",
            "Excel file cleaning — unlimited",
            "All three verification gates on every document",
            "Email support",
        ],
        sort_order=1,
    ),
    dict(
        slug="gold",
        name_ar="الباقة الذهبية",
        name_en="Gold",
        price_amount=499,
        period=Period.SEMIANNUAL,
        monthly_limit=5_000,
        features_ar=[
            "كل مزايا الباقة الفضية",
            "معالجة دفعات الملفات",
            "أولوية في المعالجة",
        ],
        features_en=[
            "Everything in Silver",
            "Batch file processing",
            "Priority processing",
        ],
        sort_order=2,
    ),
    dict(
        slug="platinum",
        name_ar="الباقة البلاتينية",
        name_en="Platinum",
        price_amount=899,
        period=Period.ANNUAL,
        monthly_limit=10_000,
        features_ar=[
            "كل مزايا الباقة الذهبية",
            "قوالب Excel مخصّصة لمؤسستك",
            "مدير حساب ودعم مباشر",
        ],
        features_en=[
            "Everything in Gold",
            "Excel templates tailored to your organisation",
            "Account manager and direct support",
        ],
        highlighted=True,
        sort_order=3,
    ),
]


async def ensure_plans() -> None:
    """Seed the price list once, on an empty database.

    Only when the collection is empty. The administrator edits these afterwards,
    and a restart that quietly rewrote their prices back to ours would be a bug
    with a bill attached.
    """
    if await Plan.find_one() is not None:
        return
    await Plan.insert_many([Plan(**fields) for fields in _STARTING_PLANS])
    logger.info("seeded %d starting plans", len(_STARTING_PLANS))
