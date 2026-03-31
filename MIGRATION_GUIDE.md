# Product CSV to Database Migration Guide

## Option 1: Run Migration Script Inside Container (Recommended)

```bash
docker compose exec api python migrate_products.py
```

This will:
- Read `products.csv`
- Parse each row
- Insert into PostgreSQL `products` table
- Display count of migrated products

---

## Option 2: Manual Migration via SQL

Connect to PostgreSQL and run:

```sql
-- Create temp table from CSV
CREATE TEMP TABLE products_import (product TEXT, price TEXT);

-- Copy CSV data (adjust path as needed)
COPY products_import FROM '/path/to/products.csv' WITH (FORMAT csv, HEADER);

-- Insert into products table
INSERT INTO products (name, price)
SELECT 
  product,
  CAST(REPLACE(REPLACE(price, 'NGN', ''), ' ', '') AS FLOAT)
FROM products_import;

-- Verify
SELECT COUNT(*) FROM products;
```

---

## Option 3: Python Script from Host Machine

Edit DATABASE_URL in `migrate_products.py` to use localhost:

```python
DATABASE_URL = "postgresql://koolbuy:koolbuy_secure_password_2026@localhost:5432/koolbuy"
```

Then run:
```bash
python migrate_products.py
```

---

## Verify Migration Success

```bash
# Connect to database
docker compose exec -T postgres psql -U koolbuy -d koolbuy

# Inside psql:
SELECT COUNT(*) FROM products;
SELECT * FROM products LIMIT 5;
```

Expected output: `count = ~10-50 products` (depending on CSV size)

---

## After Migration

1. **Test the API:**
   ```bash
   curl -X POST "http://localhost:8001/chat" \
     -H "Content-Type: application/json" \
     -d '{"session_id":"test","message":"__welcome__","user_name":"Test"}'
   ```
   
   If products are loaded, the system prompt includes them.

2. **Archive or delete products.csv** (no longer needed)
   ```bash
   rm products.csv
   # or backup: cp products.csv products.csv.backup
   ```

3. **Commit changes:**
   ```bash
   git add migrate_products.py .env
   git commit -m "Add product migration script"
   git push origin main
   ```

---

## Troubleshooting

**"ModuleNotFoundError: No module named 'models'"**
- Run from `/app` directory inside container
- Or install dependencies locally: `pip install -r requirement.txt`

**"Connection refused"**
- Ensure PostgreSQL container is running: `docker compose ps`
- Check DATABASE_URL matches your environment

**"No such file or directory: products.csv"**
- Ensure script runs from project root directory
- Check file exists: `ls -la products.csv`

---

## Next Steps

Once products are in database:
- ✅ CSV files can be archived/deleted
- ✅ All queries come from PostgreSQL
- ✅ Data is persisted across container restarts
- ✅ Can now populate via admin API or UI forms
