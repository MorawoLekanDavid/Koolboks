from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from chatbot.database import get_db
from chatbot.dependencies import get_admin_ctx, require_admin
from chatbot.models import Product

router = APIRouter(prefix="/admin/products", tags=["products"])


class ProductIn(BaseModel):
    name: str
    price: float
    image_url: Optional[str] = ""
    product_url: Optional[str] = ""
    description: Optional[str] = ""


@router.get("")
async def list_products(ctx: dict = Depends(get_admin_ctx)):
    db = get_db()
    try:
        products = db.query(Product).order_by(Product.name).all()
        return [{"id": p.id, "name": p.name, "price": p.price,
                 "image_url": p.image_url, "product_url": p.product_url,
                 "description": p.description,
                 "created_at": p.created_at.isoformat() if p.created_at else None} for p in products]
    finally:
        db.close()


@router.post("")
async def create_product(body: ProductIn, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        p = Product(name=body.name.strip(), price=body.price,
                    image_url=body.image_url or "", product_url=body.product_url or "",
                    description=body.description or "")
        db.add(p)
        db.commit()
        db.refresh(p)
        return {"id": p.id, "name": p.name, "price": p.price,
                "image_url": p.image_url, "product_url": p.product_url, "description": p.description}
    finally:
        db.close()


@router.patch("/{product_id}")
async def update_product(product_id: int, body: ProductIn, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        p = db.query(Product).filter(Product.id == product_id).first()
        if not p:
            raise HTTPException(404, "Product not found")
        p.name = body.name.strip()
        p.price = body.price
        p.image_url = body.image_url or ""
        p.product_url = body.product_url or ""
        p.description = body.description or ""
        p.updated_at = datetime.utcnow()
        db.commit()
        return {"id": p.id, "name": p.name, "price": p.price,
                "image_url": p.image_url, "product_url": p.product_url, "description": p.description}
    finally:
        db.close()


@router.delete("/{product_id}")
async def delete_product(product_id: int, ctx: dict = Depends(require_admin)):
    db = get_db()
    try:
        p = db.query(Product).filter(Product.id == product_id).first()
        if not p:
            raise HTTPException(404, "Product not found")
        db.delete(p)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


class ProductBulkIn(BaseModel):
    products: List[ProductIn]


@router.post("/bulk")
async def bulk_upsert_products(body: ProductBulkIn, ctx: dict = Depends(require_admin)):
    if not body.products:
        raise HTTPException(400, "No products provided")
    db = get_db()
    try:
        created, updated = 0, 0
        for item in body.products:
            name = item.name.strip()
            if not name:
                continue
            existing = db.query(Product).filter(Product.name == name).first()
            if existing:
                existing.price = item.price
                existing.image_url = item.image_url or ""
                existing.product_url = item.product_url or ""
                existing.description = item.description or ""
                existing.updated_at = datetime.utcnow()
                updated += 1
            else:
                db.add(Product(name=name, price=item.price,
                               image_url=item.image_url or "",
                               product_url=item.product_url or "",
                               description=item.description or ""))
                created += 1
        db.commit()
        return {"created": created, "updated": updated}
    finally:
        db.close()
