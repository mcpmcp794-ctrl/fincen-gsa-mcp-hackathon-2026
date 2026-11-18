# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# DBTITLE 1,Overview
# MAGIC %md
# MAGIC # 00 — Initialize persistent transaction IDs (run once)
# MAGIC MCP copy of Jenna's notebook: writes `workspace.default.card_transaction_mcp_with_id` (owned by Michael).
# MAGIC Changes vs original: table name, and a TIME type mapping (source `Time` is now TIME(6)).
# MAGIC Source: workspace.default.card_transaction, with exactly 15 original columns.
# MAGIC This notebook NEVER drops or overwrites the source or existing identity table.
# MAGIC It creates an identity table only if absent, then checks counts and uniqueness.
# MAGIC A Delta identity ID is persistent only while this table is preserved.

# COMMAND ----------

# DBTITLE 1,Configuration and source check
from pyspark.sql import functions as F

SOURCE_TABLE = 'workspace.default.card_transaction'
ID_TABLE = 'workspace.default.card_transaction_mcp_with_id'
EXPECTED = ['User','Card','Year','Month','Day','Time','Amount','Use Chip',
            'Merchant Name','Merchant City','Merchant State','Zip','MCC','Errors','Is_Fraud']

def q(name):
    return '`' + name.replace('`', '``') + '`'

source = spark.read.table(SOURCE_TABLE)
assert source.columns == EXPECTED, f'Source columns differ from confirmed schema: {source.columns}'
print('Source rows:', source.count())
source.printSchema()

# COMMAND ----------

# DBTITLE 1,Initialize identity table

# ============================================================
# 00 — INITIALIZE PERSISTENT TRANSACTION IDs (RUN ONCE)
#
# Source:
#   workspace.default.card_transaction
#
# Target:
#   workspace.default.card_transaction_mcp_with_id
#
# Purpose:
#   1. Read the existing credit-card transaction table.
#   2. Validate the original 15-column schema.
#   3. Create a Delta identity table.
#   4. Assign unique transaction IDs.
#   5. Preserve duplicate-valued transactions.
#   6. Validate row counts and ID uniqueness.
#
# Safety:
#   - Does NOT modify the original source.
#   - Does NOT drop existing tables.
#   - Does NOT overwrite existing data.
#   - Does NOT automatically reload existing tables.
# ============================================================

from pyspark.sql import functions as F
from pyspark.sql.types import (
    StringType,
    IntegerType,
    LongType,
    ShortType,
    ByteType,
    FloatType,
    DoubleType,
    DecimalType,
    BooleanType,
    DateType,
    TimestampType,
    TimestampNTZType,
    BinaryType
)

# ============================================================
# STEP 1 — CONFIGURATION
# ============================================================

SOURCE_TABLE = "workspace.default.card_transaction"

ID_TABLE = "workspace.default.card_transaction_mcp_with_id"

EXPECTED_COLUMNS = [
    "User",
    "Card",
    "Year",
    "Month",
    "Day",
    "Time",
    "Amount",
    "Use Chip",
    "Merchant Name",
    "Merchant City",
    "Merchant State",
    "Zip",
    "MCC",
    "Errors",
    "Is_Fraud"
]

print("=" * 65)
print("INITIALIZE PERSISTENT TRANSACTION IDs")
print("=" * 65)
print("Source:", SOURCE_TABLE)
print("Target:", ID_TABLE)

# ============================================================
# STEP 2 — SAFE SQL IDENTIFIER QUOTING
# ============================================================

def quote_identifier(name):
    """
    Safely quote a Databricks SQL identifier.

    Handles spaces and special characters.
    """
    return "`" + str(name).replace("`", "``") + "`"


def quote_table_name(table_name):
    """
    Quote a three-part Unity Catalog table name.
    """
    parts = table_name.split(".")

    if len(parts) != 3:
        raise ValueError(
            "Expected catalog.schema.table: "
            + table_name
        )

    return ".".join(
        quote_identifier(part)
        for part in parts
    )


SOURCE_SQL = quote_table_name(SOURCE_TABLE)
ID_SQL = quote_table_name(ID_TABLE)

# ============================================================
# STEP 3 — VALIDATE SOURCE TABLE
# ============================================================

if not spark.catalog.tableExists(SOURCE_TABLE):
    raise ValueError(
        f"Source table does not exist: {SOURCE_TABLE}"
    )

source = spark.read.table(SOURCE_TABLE)

print("\nSource schema:")
source.printSchema()

actual_columns = source.columns

missing_columns = [
    c for c in EXPECTED_COLUMNS
    if c not in actual_columns
]

extra_columns = [
    c for c in actual_columns
    if c not in EXPECTED_COLUMNS
]

if missing_columns or extra_columns:
    raise ValueError(
        "Source schema mismatch.\n"
        f"Missing columns: {missing_columns}\n"
        f"Extra columns: {extra_columns}\n"
        f"Actual columns: {actual_columns}"
    )

# Preserve the expected column order
source = source.select(
    *[F.col(quote_identifier(c)) for c in EXPECTED_COLUMNS]
)

print("\nSource columns validated.")

# ============================================================
# STEP 4 — CONVERT SPARK DATA TYPES TO SQL TYPES
# ============================================================

def spark_type_to_sql(data_type):
    """
    Convert Spark data types to Databricks SQL DDL types.
    """

    if isinstance(data_type, StringType):
        return "STRING"

    if isinstance(data_type, IntegerType):
        return "INT"

    if isinstance(data_type, LongType):
        return "BIGINT"

    if isinstance(data_type, ShortType):
        return "SMALLINT"

    if isinstance(data_type, ByteType):
        return "TINYINT"

    if isinstance(data_type, FloatType):
        return "FLOAT"

    if isinstance(data_type, DoubleType):
        return "DOUBLE"

    if isinstance(data_type, DecimalType):
        return (
            f"DECIMAL({data_type.precision},"
            f"{data_type.scale})"
        )

    if isinstance(data_type, BooleanType):
        return "BOOLEAN"

    if isinstance(data_type, DateType):
        return "DATE"

    if isinstance(data_type, TimestampNTZType):
        return "TIMESTAMP_NTZ"

    if isinstance(data_type, TimestampType):
        return "TIMESTAMP"

    if isinstance(data_type, BinaryType):
        return "BINARY"

    # TIME columns (source `Time` is now TIME(6)); added for the MCP rebuild.
    if type(data_type).__name__ == "TimeType":
        return data_type.simpleString().upper()

    raise TypeError(
        f"Unsupported Spark data type: {data_type}"
    )

# ============================================================
# STEP 5 — CHECK EXISTING TARGET TABLE
# ============================================================

target_exists = spark.catalog.tableExists(ID_TABLE)

print("\nTarget table exists:", target_exists)

# ============================================================
# STEP 6 — CREATE TARGET TABLE IF MISSING
# ============================================================

if not target_exists:

    print("\nCreating persistent Delta identity table...")

    column_definitions = ",\n".join(
        (
            f"{quote_identifier(field.name)} "
            f"{spark_type_to_sql(field.dataType)}"
        )
        for field in source.schema.fields
    )

    create_sql = f"""
    CREATE TABLE {ID_SQL} (
        transaction_id BIGINT
            GENERATED ALWAYS AS IDENTITY,

        {column_definitions}
    )
    USING DELTA
    TBLPROPERTIES (
        'delta.columnMapping.mode' = 'name'
    )
    """

    spark.sql(create_sql)

    print("Created:", ID_TABLE)

    # ========================================================
    # STEP 7 — INSERT SOURCE ROWS ONCE
    # ========================================================

    print("\nInserting source transactions...")

    source.createOrReplaceTempView(
        "card_transaction_initial_load"
    )

    columns_sql = ", ".join(
        quote_identifier(c)
        for c in EXPECTED_COLUMNS
    )

    insert_sql = f"""
    INSERT INTO {ID_SQL} (
        {columns_sql}
    )
    SELECT
        {columns_sql}
    FROM card_transaction_initial_load
    """

    spark.sql(insert_sql)

    print("Initial source insertion completed.")

else:

    print(
        "\nExisting identity table found. "
        "No insertion or overwrite performed."
    )

# ============================================================
# STEP 8 — VALIDATE TARGET SCHEMA
# ============================================================

target = spark.table(ID_TABLE)

print("\nTarget schema:")
target.printSchema()

required_target_columns = [
    "transaction_id"
] + EXPECTED_COLUMNS

missing_target_columns = [
    c for c in required_target_columns
    if c not in target.columns
]

if missing_target_columns:
    raise ValueError(
        "Existing target table has an incompatible schema. "
        f"Missing columns: {missing_target_columns}"
    )

if len(target.columns) != len(required_target_columns):
    raise ValueError(
        "Unexpected columns in target table: "
        f"{target.columns}"
    )

print("Target schema validated.")

# ============================================================
# STEP 9 — VALIDATE ROW COUNTS
# ============================================================

print("\nCounting source and target records...")

source_count = source.count()

target_count = target.count()

print("Source rows:", f"{source_count:,}")
print("Target rows:", f"{target_count:,}")

if source_count != target_count:
    raise ValueError(
        "Source and target row counts do not match.\n"
        f"Source: {source_count:,}\n"
        f"Target: {target_count:,}\n"
        "The target may be empty, partially initialized, "
        "or based on a different source snapshot. "
        "No automatic reload was performed."
    )

# ============================================================
# STEP 10 — VALIDATE UNIQUE TRANSACTION IDs
# ============================================================

print("\nValidating transaction IDs...")

id_stats = target.agg(
    F.count("*").alias("total_rows"),

    F.count("transaction_id").alias(
        "non_null_ids"
    ),

    F.countDistinct("transaction_id").alias(
        "unique_ids"
    ),

    F.min("transaction_id").alias(
        "min_id"
    ),

    F.max("transaction_id").alias(
        "max_id"
    )
).first()

total_rows = id_stats["total_rows"]
non_null_ids = id_stats["non_null_ids"]
unique_ids = id_stats["unique_ids"]

print("Total rows:", f"{total_rows:,}")
print("Non-null IDs:", f"{non_null_ids:,}")
print("Unique IDs:", f"{unique_ids:,}")
print("Minimum ID:", id_stats["min_id"])
print("Maximum ID:", id_stats["max_id"])

if total_rows != non_null_ids:
    raise ValueError(
        "Null transaction IDs detected."
    )

if total_rows != unique_ids:
    raise ValueError(
        "Duplicate transaction IDs detected."
    )

# ============================================================
# STEP 11 — VALIDATE ORIGINAL COLUMN VALUES
# ============================================================

print("\nChecking original source columns...")

for column_name in EXPECTED_COLUMNS:

    source_nulls = source.filter(
        F.col(quote_identifier(column_name)).isNull()
    ).count()

    target_nulls = target.filter(
        F.col(quote_identifier(column_name)).isNull()
    ).count()

    if source_nulls != target_nulls:
        raise ValueError(
            f"Null-count mismatch for {column_name}: "
            f"source={source_nulls}, "
            f"target={target_nulls}"
        )

print("Source/target null counts validated.")

# ============================================================
# STEP 12 — FINAL RESULT
# ============================================================

print("\n" + "=" * 65)
print("PERSISTENT TRANSACTION ID INITIALIZATION COMPLETE")
print("=" * 65)

print("Source:", SOURCE_TABLE)
print("Target:", ID_TABLE)
print("Source rows:", f"{source_count:,}")
print("Target rows:", f"{target_count:,}")
print("Unique IDs:", f"{unique_ids:,}")

print("\nAll validation checks passed.")

display(
    target.select(
        "transaction_id",
        "User",
        "Card",
        "Year",
        "Month",
        "Day",
        "Time",
        "Amount",
        "Merchant Name",
        "Errors",
        "Is_Fraud"
    ).limit(20)
)
# This is a one-time initialization. Existing table is NEVER automatically reloaded.
if not spark.catalog.tableExists(ID_TABLE):
    defs = ',\n'.join(f'{q(f.name)} {f.dataType.simpleString()}' for f in source.schema.fields)
    spark.sql(f'''CREATE TABLE {ID_TABLE} (
        transaction_id BIGINT GENERATED ALWAYS AS IDENTITY,
        {defs}
    ) USING DELTA
    TBLPROPERTIES ('delta.columnMapping.mode' = 'name')''')
    cols = ', '.join(q(c) for c in EXPECTED)
    source.createOrReplaceTempView('card_transaction_id_initial_load')
    spark.sql(f'''INSERT INTO {ID_TABLE} ({cols})
                  SELECT {cols} FROM card_transaction_id_initial_load''')
    print('Created and initialized', ID_TABLE)
else:
    print('Existing identity table found; no insert or overwrite performed.')

# COMMAND ----------


from pyspark.sql import functions as F

# ============================================================
# VALIDATE PERSISTENT TRANSACTION IDs
# ============================================================

identity = spark.read.table(ID_TABLE)

# 1. Compare row counts
source_count = source.count()
identity_count = identity.count()

# 2. Check null transaction IDs
null_id_exists = (
    identity
    .filter(F.col("transaction_id").isNull())
    .limit(1)
    .count() > 0
)

# 3. Check duplicate transaction IDs
duplicate_id_exists = (
    identity
    .groupBy("transaction_id")
    .count()
    .filter(F.col("count") > 1)
    .limit(1)
    .count() > 0
)

# 4. Verify original source columns
missing_columns = [
    c for c in EXPECTED
    if c not in identity.columns
]

# 5. Validate
assert identity_count == source_count, (
    f"Row count mismatch: "
    f"source={source_count}, "
    f"identity={identity_count}"
)

assert not null_id_exists, (
    "Identity table contains null transaction IDs"
)

assert not duplicate_id_exists, (
    "Identity table contains duplicate transaction IDs"
)

assert not missing_columns, (
    f"Identity table missing columns: {missing_columns}"
)

print("PASS: Source and identity row counts match.")
print("PASS: Transaction IDs are unique and non-null.")
print("PASS: Original source columns are preserved.")

# ============================================================
# OPTIONAL: EXACT SOURCE ROW COMPARISON
# ============================================================

VERIFY_EXACT_SOURCE_ROWS = True

if VERIFY_EXACT_SOURCE_ROWS:

    left = source.select(
        *[F.col(q(c)) for c in EXPECTED]
    )

    right = identity.select(
        *[F.col(q(c)) for c in EXPECTED]
    )

    # Compare both directions to preserve duplicate counts
    missing_from_identity = (
        left.exceptAll(right)
        .limit(1)
        .count()
    )

    extra_in_identity = (
        right.exceptAll(left)
        .limit(1)
        .count()
    )

    assert missing_from_identity == 0, (
        "Source contains rows missing from identity table"
    )

    assert extra_in_identity == 0, (
        "Identity table contains rows absent from source"
    )

    print("PASS: Exact source-row equivalence verified.")

print("\nIdentity table validation completed.")