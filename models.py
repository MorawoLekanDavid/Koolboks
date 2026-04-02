from sqlalchemy import Column, String, Float, DateTime, Integer, Boolean, create_engine
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()


class Product(Base):
    """Product inventory model"""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), index=True)
    price = Column(Float)
    image_url = Column(String(512), nullable=True)
    product_url = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow)


class Lead(Base):
    """Lead/customer data model"""
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=True)
    phone = Column(String(20), unique=True, index=True)
    business = Column(String(255), nullable=True)
    product_interest = Column(String(255), nullable=True)
    amount = Column(String(100), nullable=True)
    payment_plan = Column(String(255), nullable=True)
    pain_point = Column(String(512), nullable=True)
    power_type = Column(String(50), nullable=True)
    address = Column(String(512), nullable=True)
    active_duration = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow)


def init_db(database_url: str):
    """Initialize database tables"""
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(bind=engine)
    return engine
