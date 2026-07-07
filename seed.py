"""Seed a demo restaurant so the whole system runs out of the box.

    python seed.py
"""
from datetime import time

from sqlmodel import Session, SQLModel, select

from api.main import engine
from api.models import Business, KBEntry, ServicePeriod


def seed():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        if s.exec(select(Business).where(Business.slug == "luigis-carlton")).first():
            print("already seeded")
            return

        biz = Business(
            slug="luigis-carlton",
            name="Luigi's Trattoria",
            address="123 Lygon Street, Carlton, Melbourne",
            owner_mobile="+61400000000",
            phone_forward_to="+61390000000",
            covers_per_slot=12,
            max_party_size=8,
            persona_notes="Warm, a little playful, proudly Italian. Never rushed.",
        )
        s.add(biz)
        s.commit()
        s.refresh(biz)

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

        s.commit()
        print("seeded: luigis-carlton")


if __name__ == "__main__":
    seed()
