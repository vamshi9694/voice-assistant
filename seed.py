"""Seed TWO demo restaurants (multi-tenant from the first boot) with routed
phone numbers, language config, and per-tenant KBs.

    python seed.py

Set the numbers to your real Twilio numbers via env before seeding, or update
later with POST /admin/numbers:
    LUIGIS_NUMBER=+61370000001 TACOS_NUMBER=+61370000002 python seed.py
"""
import os
from datetime import time

from sqlmodel import Session, SQLModel, select

from api.auth import hash_password
from api.main import engine
from api.models import (
    Business, KBEntry, MenuItem, PhoneNumber, ServicePeriod, User, UserRole,
)


def seed():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        if s.exec(select(Business).where(Business.slug == "luigis-carlton")).first():
            print("already seeded")
            return

        # ------------------- Tenant 1: Luigi's (en, dinner-led) -------------------
        biz = Business(
            slug="luigis-carlton",
            name="Luigi's Trattoria",
            address="123 Lygon Street, Carlton, Melbourne",
            website="https://luigis-carlton.example.com",
            owner_mobile="+61400000000",
            manager_phone="+61400000001",
            manager_email="manager@luigis.example.com",
            phone_forward_to="+61390000000",
            covers_per_slot=12,
            max_party_size=8,
            reservation_notes="No bookings after 8:30pm Fridays; window seats on request only.",
            orders_enabled=False,
            order_policy_notes="No phone orders — dine-in and reservations only.",
            default_language="en",
            enabled_languages='["en"]',
            persona_notes="Warm, a little playful, proudly Italian. Never rushed.",
            escalation_rules="Escalate complaints, media enquiries, and functions over 20 people "
                             "to the manager immediately (urgent message).",
        )
        s.add(biz)
        s.commit()
        s.refresh(biz)

        s.add(PhoneNumber(
            e164=os.getenv("LUIGIS_NUMBER", "+61370000001"),
            business_id=biz.id, label="main line",
        ))

        # Tue-Sun dinner; Fri-Sun also lunch. Closed Mondays.
        for dow in range(1, 7):  # Tue(1)..Sun(6)
            s.add(ServicePeriod(
                business_id=biz.id, day_of_week=dow, name="dinner",
                opens=time(17, 30), last_seating=time(21, 0), closes=time(22, 30),
            ))
        for dow in (4, 5, 6):  # Fri, Sat, Sun lunch
            s.add(ServicePeriod(
                business_id=biz.id, day_of_week=dow, name="lunch",
                opens=time(12, 0), last_seating=time(14, 0), closes=time(15, 0),
            ))

        kb = {
            "parking": "Paid street parking on Lygon St; free 2-hour parking on Faraday St after 6pm.",
            "byo": "BYO wine only, $15 corkage per bottle. Full bar available.",
            "dietary": "Gluten-free pasta available for most dishes. Vegan menu on request. "
                       "Kitchen handles nuts and shellfish, so traces are possible.",
            "gift cards": "Gift cards available in-store or on our website, any amount.",
            "kids": "Kids menu available; high chairs on request.",
            "large groups": "Parties over 8 are handled as private bookings — we take a message "
                            "and the manager calls back within a day.",
            "specials": "Tuesday: half-price pasta. Sunday lunch: $49 set menu.",
        }
        for topic, answer in kb.items():
            s.add(KBEntry(business_id=biz.id, topic=topic, answer=answer))

        menu1 = [
            ("Antipasti", "Garlic bread", 9.0, "V"),
            ("Antipasti", "Burrata with heirloom tomatoes", 18.0, "GF, V"),
            ("Pasta", "Spaghetti carbonara", 28.0, ""),
            ("Pasta", "Pappardelle with slow-cooked ragu", 32.0, ""),
            ("Pasta", "Gnocchi gorgonzola", 29.0, "V"),
            ("Mains", "Chicken saltimbocca", 36.0, "GF"),
            ("Dolci", "Tiramisu", 15.0, "V"),
        ]
        for section, name, price, dietary in menu1:
            s.add(MenuItem(business_id=biz.id, section=section, name=name,
                           price=price, dietary=dietary))

        # --------------- Tenant 2: Tacos El Rey (bilingual, takeout) ---------------
        biz2 = Business(
            slug="tacos-el-rey",
            name="Tacos El Rey",
            address="45 Sydney Road, Brunswick, Melbourne",
            website="https://tacos-el-rey.example.com",
            owner_mobile="+61400000100",
            manager_phone="+61400000101",
            manager_email="manager@tacoselrey.example.com",
            covers_per_slot=8,
            max_party_size=6,
            orders_enabled=True,
            order_pickup_minutes=15,
            order_policy_notes="Phone orders for PICKUP only, pay in store. No delivery.",
            default_language="en",
            enabled_languages='["en","es"]',
            auto_detect_language=True,
            persona_notes="Upbeat, casual, bilingual. Short answers.",
            escalation_rules="Escalate catering requests and complaints to the manager.",
        )
        s.add(biz2)
        s.commit()
        s.refresh(biz2)

        s.add(PhoneNumber(
            e164=os.getenv("TACOS_NUMBER", "+61370000002"),
            business_id=biz2.id, label="main line",
        ))

        for dow in range(0, 7):  # open every day, all-day service
            s.add(ServicePeriod(
                business_id=biz2.id, day_of_week=dow, name="all-day",
                opens=time(11, 0), last_seating=time(21, 30), closes=time(22, 0),
            ))

        kb2 = {
            "parking": "Free 1-hour parking on Sydney Rd side streets.",
            "salsa heat": "Salsas from mild to muy picante — ask for a taste.",
            "catering": "Catering for 10+ people with 48 hours notice — the manager calls back to confirm.",
        }
        for topic, answer in kb2.items():
            s.add(KBEntry(business_id=biz2.id, topic=topic, answer=answer))

        menu2 = [
            ("Tacos", "Al pastor taco", 6.5, ""),
            ("Tacos", "Carnitas taco", 6.5, "GF"),
            ("Tacos", "Baja fish taco", 7.5, ""),
            ("Tacos", "Mushroom taco", 6.0, "V, GF"),
            ("Burritos", "Chicken burrito", 16.0, ""),
            ("Burritos", "Veggie burrito", 15.0, "V"),
            ("Sides", "Chips and guacamole", 9.0, "V, GF"),
            ("Drinks", "Horchata", 5.5, "V"),
        ]
        for section, name, price, dietary in menu2:
            s.add(MenuItem(business_id=biz2.id, section=section, name=name,
                           price=price, dietary=dietary))

        # ------------------------------ users ------------------------------
        s.add(User(email="admin@platform.local",
                   password_hash=hash_password(os.getenv("ADMIN_PASSWORD", "admin123")),
                   role=UserRole.platform_admin))
        s.add(User(email="owner@luigis.local",
                   password_hash=hash_password(os.getenv("OWNER_PASSWORD", "owner123")),
                   role=UserRole.tenant_admin, business_id=biz.id))
        s.add(User(email="owner@tacos.local",
                   password_hash=hash_password(os.getenv("OWNER_PASSWORD", "owner123")),
                   role=UserRole.tenant_admin, business_id=biz2.id))

        s.commit()
        print("seeded: luigis-carlton (+61370000001), tacos-el-rey (+61370000002)")
        print("logins: admin@platform.local/admin123, owner@luigis.local/owner123, owner@tacos.local/owner123")


if __name__ == "__main__":
    seed()
