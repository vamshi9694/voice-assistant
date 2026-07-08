"""Seed a REAL demo tenant: Namaste Taste of India (Lilburn, GA).

Data sourced from namastecw.com (address, hours, menu, prices, policies) so the
AI answers as a real restaurant — much stronger for client demos than Luigi's.

Idempotent: re-running refreshes hours/menu/KB and re-points the demo number.

    python seed_namaste.py

The demo phone number defaults to your Twilio line; override with env:
    NAMASTE_NUMBER=+14063568133 python seed_namaste.py
"""
import os
from datetime import time

from sqlmodel import Session, SQLModel, delete, select

from api.auth import hash_password
from api.main import engine
from api.models import (
    Business, KBEntry, MenuItem, PhoneNumber, ServicePeriod, TenantStatus,
    User, UserRole,
)

SLUG = "namaste"
NUMBER = os.getenv("NAMASTE_NUMBER", "+14063568133")

# (section, name, price, dietary/notes)  — real items + prices from namastecw.com
MENU = [
    # Non-Vegetarian Appetizers (dinner grill, Tue–Thu 5–9pm)
    ("Non-Veg Appetizers", "Chicken 65", 12.99, ""),
    ("Non-Veg Appetizers", "Dragon Chicken", 12.99, "spicy"),
    ("Non-Veg Appetizers", "Chicken Kondattam", 10.99, "spicy"),
    ("Non-Veg Appetizers", "Koonthal Roast (squid)", 14.99, "spicy"),
    ("Non-Veg Appetizers", "Kallumakkaya Fry (mussels)", 14.99, "spicy"),
    ("Non-Veg Appetizers", "Masala Omelette", 8.99, "spicy"),
    # Vegetarian Appetizers
    ("Veg Appetizers", "Paneer Tikka", 9.99, "V"),
    ("Veg Appetizers", "Taco Dosa (potato & chickpea)", 10.99, "V"),
    ("Veg Appetizers", "Dosa Bonda", 9.99, "V"),
    ("Veg Appetizers", "Gobi Kondattam (crispy cauliflower)", 10.99, "V, spicy"),
    ("Veg Appetizers", "Tandoori Broccoli", 10.99, "V"),
    ("Veg Appetizers", "Manchurian (paneer or cauliflower)", 10.99, "V, spicy"),
    # Soups
    ("Soups", "Vegetable Rasam", 4.99, "V"),
    ("Soups", "Chicken Rasam", 6.99, ""),
    # Tandoor Clay Oven (dinner only, Tue–Thu 5–9pm)
    ("Tandoor", "Tandoori Chicken", 14.99, ""),
    ("Tandoor", "Murgh Tikka", 14.99, ""),
    ("Tandoor", "Malai Tikka", 16.99, ""),
    ("Tandoor", "Tangadi Kebab", 16.99, ""),
    ("Tandoor", "Lamb Seekh Kebab", 16.99, ""),
    ("Tandoor", "Ajwani Salmon", 16.99, ""),
    ("Tandoor", "Lamb Chops Hariyali", 19.99, "spicy"),
    # Biryani
    ("Biryani", "Malabar Biriyani", 15.99, "spicy"),
    ("Biryani", "Hyderabadi Dum Biriyani", 15.99, "spicy"),
    ("Biryani", "Afghani Chicken Biriyani", 16.99, ""),
    ("Biryani", "Malabar Prawns Biriyani", 15.99, ""),
    ("Biryani", "Beef Kappa Biriyani", 15.99, ""),
    ("Biryani", "Bucket Biriyani (family size)", 42.99, ""),
    # Non-Veg Curry (served with basmati rice)
    ("Non-Veg Curry", "Chicken Tikka Masala", 14.99, ""),
    ("Non-Veg Curry", "Chicken Kadai", 14.99, "spicy"),
    ("Non-Veg Curry", "Kurma", 14.99, ""),
    ("Non-Veg Curry", "Lamb Rogan Josh", 17.99, ""),
    ("Non-Veg Curry", "Lamb Madras", 17.99, "spicy"),
    ("Non-Veg Curry", "Achayan's Fish Curry", 15.99, "spicy"),
    ("Non-Veg Curry", "Goan Shrimp Balchao", 15.99, "spicy"),
    ("Non-Veg Curry", "Moilee (fish/shrimp coconut curry)", 15.99, "spicy"),
    # Vegetarian Curry (served with basmati rice)
    ("Veg Curry", "Paneer Tikka Masala", 12.99, "V"),
    ("Veg Curry", "Saag Paneer", 12.99, "V"),
    ("Veg Curry", "Kadai Paneer", 12.99, "V, spicy"),
    ("Veg Curry", "Chana Masala", 11.99, "V"),
    ("Veg Curry", "Dal Tadka", 10.99, "V"),
    ("Veg Curry", "Aloo Gobi Masala", 11.99, "V"),
    ("Veg Curry", "Eggplant Coconut Curry", 11.99, "V"),
    ("Veg Curry", "Spinach Kofta", 12.99, "V"),
    # Dosa
    ("Dosa", "Masala Dosa", 14.99, "V"),
    ("Dosa", "Mysore Masala Dosa", 14.99, "V"),
    ("Dosa", "Ghee Roast Dosa", 14.99, "V"),
    ("Dosa", "Spring Dosa", 14.99, "V"),
    ("Dosa", "Cheese Dosa", 14.99, "V"),
    ("Dosa", "Protein Dosa", 15.99, ""),
    # South Indian breads/combos
    ("South Indian", "Poori Bhaji", 15.99, "V"),
    ("South Indian", "Channa Bhatura", 15.99, "V"),
    # Indo-Chinese
    ("Indo-Chinese", "Fried Rice", 12.99, "spicy"),
    ("Indo-Chinese", "Stir Fried Noodles", 12.99, "spicy"),
    ("Indo-Chinese", "Nasi Goreng Seafood Fried Rice", 16.99, "spicy"),
    # Naan / Bread
    ("Naan / Bread", "Plain Naan", 1.99, "V"),
    ("Naan / Bread", "Butter Naan", 2.49, "V"),
    ("Naan / Bread", "Garlic Naan", 2.99, "V"),
    ("Naan / Bread", "Rosemary Garlic Naan", 3.29, "V"),
    ("Naan / Bread", "Appam", 2.29, "V"),
    # Rice & Extras
    ("Rice & Extras", "Basmati Rice", 1.99, "V"),
    ("Rice & Extras", "Coconut Rice", 11.99, "V"),
    ("Rice & Extras", "Tomato Rice", 11.99, "V"),
    ("Rice & Extras", "Sambar", 2.99, "V"),
    ("Rice & Extras", "Raita", 1.49, "V"),
    ("Rice & Extras", "Pappadom (2 pieces)", 1.49, "V"),
    # Kid's Menu
    ("Kid's Menu", "Kid's Chicken Tikka Pasta", 12.99, ""),
    ("Kid's Menu", "Kid's Paneer Tikka Penne Pasta", 10.99, "V"),
    ("Kid's Menu", "Kid's Cheese Dosa", 6.99, "V"),
    ("Kid's Menu", "Kid's Hakka Noodles", 10.99, "V"),
    # Dessert
    ("Dessert", "Gulab Jamun", 4.99, "V"),
    ("Dessert", "Rasmalai", 6.99, "V"),
    # Beverages
    ("Beverages", "Mango Lassi (8 oz)", 5.49, "V"),
    ("Beverages", "Sweet Lassi", 4.99, "V"),
    ("Beverages", "Masala Chai", 2.99, "V"),
    ("Beverages", "Rose Milk", 4.99, "V"),
    ("Beverages", "Coffee", 2.99, "V"),
    ("Beverages", "Fountain Drink", 2.49, "V"),
    ("Beverages", "Bottled Water", 1.99, "V"),
]

# Truthful FAQ only — everything here is stated on namastecw.com.
KB = {
    "location": "We're at 4230 Lawrenceville Hwy, Suite 16, Lilburn, GA 30047.",
    "hours": "We're open Tuesday through Saturday 11am to 9pm, Sunday noon to 9pm, "
             "and closed on Mondays.",
    "popular dishes": "We're known for our dosas, biryanis, tandoori dishes, "
                      "butter chicken and chicken tikka masala, paneer dishes, and fresh naan.",
    "vegetarian and vegan": "We have a large vegetarian selection and vegan options — "
                            "just let us know and the kitchen will guide you.",
    "takeout and delivery": "Yes, we offer both takeout and delivery. Phone orders are for "
                            "pickup; for delivery you can order on our website or app.",
    "catering": "Yes, we cater events, office lunches, birthdays and more. I can take your "
                "name, number and event details and have the team call you back.",
    "private dining and events": "We have a private dining room and host events. Let me take "
                                 "your details and the team will follow up to arrange it.",
    "all you can eat": "We do offer an all-you-can-eat option — let me take your name and "
                       "number and the team will confirm the current days and pricing.",
    "rewards": "We have a rewards program — you earn points every time you order online and "
               "can redeem them for free food.",
    "gift cards": "Yes, we offer gift cards — you can get them through our website.",
    "tandoor timing": "Our tandoor clay-oven grill items and appetizers are available in the "
                      "evenings, Tuesday through Thursday from 5 to 9pm.",
    "contact": "You can reach us at namaste4230@gmail.com, and we're on Instagram at "
               "namaste_lilburn.",
}


def seed():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        biz = s.exec(select(Business).where(Business.slug == SLUG)).first()
        fields = dict(
            slug=SLUG,
            name="Namaste Taste of India",
            status=TenantStatus.active,
            industry="restaurant",
            timezone="America/New_York",
            address="4230 Lawrenceville Hwy, Suite 16, Lilburn, GA 30047",
            website="https://namastecw.com",
            owner_mobile="+14706573306",
            manager_phone="+14706573306",
            manager_email="namaste4230@gmail.com",
            phone_forward_to="+14706573306",
            greeting="Thanks for calling Namaste Taste of India in Lilburn — how can I help you?",
            covers_per_slot=16,
            max_party_size=10,
            reservation_notes="Reservations recommended for weekends and larger groups. "
                              "Tandoor grill and appetizers are dinner only (Tue–Thu 5–9pm).",
            orders_enabled=True,
            order_pickup_minutes=20,
            order_policy_notes="Phone orders are pickup only, pay at the restaurant. "
                               "For delivery, order on our website or app.",
            default_language="en",
            enabled_languages='["en"]',
            auto_detect_language=False,
            persona_notes="Warm, welcoming, and professional — proud of authentic North & "
                          "South Indian cooking. Helpful and efficient, never rushed.",
            escalation_rules="Escalate catering, private dining/events, parties over 10, and "
                             "any complaint to the manager as an urgent message.",
        )
        if biz:
            for k, v in fields.items():
                setattr(biz, k, v)
            s.add(biz)
            s.commit(); s.refresh(biz)
            # wipe child rows so re-running refreshes cleanly
            s.exec(delete(ServicePeriod).where(ServicePeriod.business_id == biz.id))
            s.exec(delete(MenuItem).where(MenuItem.business_id == biz.id))
            s.exec(delete(KBEntry).where(KBEntry.business_id == biz.id))
            s.commit()
            print(f"updated existing tenant '{SLUG}' (id={biz.id})")
        else:
            biz = Business(**fields)
            s.add(biz)
            s.commit(); s.refresh(biz)
            print(f"created tenant '{SLUG}' (id={biz.id})")

        # Hours: Tue(1)–Sat(5) 11:00–21:00, Sun(6) 12:00–21:00, Mon(0) closed.
        for dow in range(1, 6):  # Tue..Sat
            s.add(ServicePeriod(business_id=biz.id, day_of_week=dow, name="all-day",
                                opens=time(11, 0), last_seating=time(20, 30), closes=time(21, 0)))
        s.add(ServicePeriod(business_id=biz.id, day_of_week=6, name="all-day",
                            opens=time(12, 0), last_seating=time(20, 30), closes=time(21, 0)))

        for section, name, price, dietary in MENU:
            s.add(MenuItem(business_id=biz.id, section=section, name=name,
                           price=price, dietary=dietary, source="website",
                           source_url="https://namastecw.com/menu"))

        for topic, answer in KB.items():
            s.add(KBEntry(business_id=biz.id, topic=topic, answer=answer,
                          source_url="https://namastecw.com"))

        # Route the demo number to this tenant (move it if it exists elsewhere).
        pn = s.exec(select(PhoneNumber).where(PhoneNumber.e164 == NUMBER)).first()
        if pn:
            pn.business_id = biz.id
            pn.active = True
            pn.label = "demo line"
            s.add(pn)
            print(f"re-pointed {NUMBER} -> {SLUG}")
        else:
            s.add(PhoneNumber(e164=NUMBER, business_id=biz.id, label="demo line"))
            print(f"assigned {NUMBER} -> {SLUG}")

        # Owner login for the dashboard (idempotent).
        if not s.exec(select(User).where(User.email == "owner@namaste.local")).first():
            s.add(User(email="owner@namaste.local",
                       password_hash=hash_password(os.getenv("OWNER_PASSWORD", "owner123")),
                       role=UserRole.tenant_admin, business_id=biz.id))

        s.commit()
        print(f"seeded {len(MENU)} menu items, {len(KB)} FAQ topics, hours Tue–Sun.")
        print(f"demo: call {NUMBER}  |  dashboard login owner@namaste.local/owner123")


if __name__ == "__main__":
    seed()
