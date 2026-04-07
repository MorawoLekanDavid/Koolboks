#!/usr/bin/env python3
"""
Migrate products from product_catalog.csv and leads from leads.csv to PostgreSQL.
"""
import csv
import os
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Product, Lead, Base

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://koolbuy:koolbuy_secure_password_2026@localhost:5432/koolbuy")
PRODUCTS_CSV = os.path.join(os.path.dirname(__file__), "product_catalog.csv")
LEADS_CSV = os.path.join(os.path.dirname(__file__), "leads.csv")


def migrate_products(session):
    products_added = 0
    with open(PRODUCTS_CSV, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            vals = list(row.values())
            if len(vals) < 4:
                continue

            name = str(vals[0]).strip()
            if not name or name.startswith('©'):
                continue

            price_str = str(vals[1]).replace(',', '').strip()
            try:
                price = float(price_str)
            except ValueError:
                print(f"  Skipping invalid price: {price_str}")
                price = 0.0

            img_url = str(vals[2]).strip()
            description = str(vals[3]).strip()

            session.add(Product(
                name=name,
                price=price,
                image_url=img_url if img_url else None,
                description=description,
                product_url=None,
            ))
            products_added += 1
            print(f"  [product] {name} @ {price:,.0f} NGN")

    return products_added


def migrate_leads(session):
    if not os.path.exists(LEADS_CSV):
        print("  leads.csv not found, skipping.")
        return 0

    leads_added = 0
    seen_phones = set()

    with open(LEADS_CSV, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            phone = str(row.get("phone", "")).strip()
            if not phone:
                continue
            # Skip duplicate phones (keep first occurrence)
            if phone in seen_phones:
                print(f"  [lead] duplicate phone {phone}, skipping")
                continue
            seen_phones.add(phone)

            # Parse timestamp -> created_at
            ts_raw = str(row.get("timestamp", "")).strip()
            created_at = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    created_at = datetime.strptime(ts_raw, fmt)
                    break
                except ValueError:
                    pass

            session.add(Lead(
                name=str(row.get("name", "")).strip() or None,
                phone=phone,
                business=str(row.get("business", "")).strip() or None,
                product_interest=str(row.get("product_interest", "")).strip() or None,
                amount=str(row.get("amount", "")).strip() or None,
                payment_plan=str(row.get("payment_plan", "")).strip() or None,
                pain_point=str(row.get("pain_point", "")).strip() or None,
                power_type=str(row.get("power_type", "")).strip() or None,
                address=str(row.get("address", "")).strip() or None,
                active_duration=str(row.get("active_duration", "")).strip() or None,
                created_at=created_at,
            ))
            leads_added += 1
            print(f"  [lead]    {row.get('name', '')} | {phone}")

    return leads_added


def run():
    engine = create_engine(DATABASE_URL, echo=False)

    # Drop and recreate all tables cleanly
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    print("Tables dropped and recreated.")

    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        print("\nMigrating products...")
        p = migrate_products(session)

        print("\nMigrating leads...")
        l = migrate_leads(session)

        session.commit()
        print(f"\nDone. {p} products | {l} leads migrated.")
    except Exception as e:
        session.rollback()
        print(f"Migration failed: {e}")
        raise
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    run()
