#!/usr/bin/env python3
"""
Migrate products from CSV to PostgreSQL database.
"""
import csv
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Product, Base

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://koolbuy:koolbuy_secure_password_2026@localhost:5432/koolbuy")
CSV_FILE = os.path.join(os.path.dirname(__file__), "products.csv")


def migrate_products():
    """Read products.csv and insert into database"""

    # Create engine and tables
    engine = create_engine(DATABASE_URL, echo=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Read CSV
        products_added = 0
        with open(CSV_FILE, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Clean price: remove "NGN" and whitespace
                price_str = row.get('price', '0').replace('NGN', '').strip()
                try:
                    price = float(price_str)
                except ValueError:
                    print(f"⚠️  Skipping invalid price: {price_str}")
                    price = 0.0

                # Create product
                product = Product(
                    name=row.get('product', '').strip(),
                    price=price,
                    image_url=None,
                    product_url=None,
                )
                session.add(product)
                products_added += 1
                print(f"✅ Added: {product.name} @ {price} NGN")

        # Commit all
        session.commit()
        print(
            f"\n🎉 Successfully migrated {products_added} products to database!")

    except Exception as e:
        session.rollback()
        print(f"❌ Migration failed: {e}")
        raise
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    migrate_products()
