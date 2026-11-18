# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# DBTITLE 1,Overview
# MAGIC %md
# MAGIC # 01 — Prepare normalized credit-card transactions
# MAGIC MCP copy of Jenna's notebook: writes `card_transaction_mcp_*` tables (owned by Michael).
# MAGIC Changes vs original: table names, and save mode `'error'` (Spark Connect rejects `'errorifexists'`).
# MAGIC First run `00_initialize_transaction_ids_mcp`. This notebook reads the persistent ID table,
# MAGIC never creates IDs from content hashes, and never changes the original 15-column source.
# MAGIC Existing output tables are not overwritten unless REBUILD_DERIVED_TABLES is explicitly set True.

# COMMAND ----------

# DBTITLE 1,Configuration and source checks
from pyspark.sql import functions as F

SOURCE_TABLE = 'workspace.default.card_transaction'
ID_TABLE = 'workspace.default.card_transaction_mcp_with_id'
CLEAN_TABLE = 'workspace.default.card_transaction_mcp_clean'
LABEL_TABLE = 'workspace.default.card_transaction_mcp_evaluation_labels'
REJECT_TABLE = 'workspace.default.card_transaction_mcp_rejected_rows'
MAX_ROWS = None  # None recommended for final project; a sample is for development only.
REBUILD_DERIVED_TABLES = False  # Set True only after reviewing downstream dependencies.
EXPECTED = ['User','Card','Year','Month','Day','Time','Amount','Use Chip',
            'Merchant Name','Merchant City','Merchant State','Zip','MCC','Errors','Is_Fraud']

def q(name):
    return '`' + name.replace('`', '``') + '`'

raw = spark.read.table(SOURCE_TABLE)
assert raw.columns == EXPECTED, f'Unexpected source columns: {raw.columns}'
assert spark.catalog.tableExists(ID_TABLE), 'Run 00_initialize_transaction_ids_mcp first'
source = spark.read.table(ID_TABLE)
assert all(c in source.columns for c in EXPECTED + ['transaction_id'])
assert source.count() == raw.count(), 'Source and persistent ID table counts differ'
assert source.filter(F.col('transaction_id').isNull()).limit(1).count() == 0
assert source.groupBy('transaction_id').count().filter('count > 1').limit(1).count() == 0
print('Using persistent identity table:', ID_TABLE)

# COMMAND ----------

# TabFormer-style source: 15 exact original column names, including Errors and Is_Fraud.
clock = F.trim(F.col(q('Time')).cast('string'))
clock = F.when(clock.rlike(r'^\d{1,2}:\d{2}$'), F.concat(clock, F.lit(':00'))).otherwise(clock)
transaction_time = F.to_timestamp(
    F.concat_ws(' ', F.format_string('%04d-%02d-%02d',
        F.col(q('Year')).cast('int'), F.col(q('Month')).cast('int'),
        F.col(q('Day')).cast('int')), clock),
    'yyyy-MM-dd HH:mm:ss')

raw_label = F.lower(F.trim(F.col(q('Is_Fraud')).cast('string')))
label = (F.when(raw_label.isin('yes','true','1'), F.lit(1))
         .when(raw_label.isin('no','false','0'), F.lit(0)))

prepared = source.select(
    F.col('transaction_id').cast('string').alias('transaction_id'),
    F.trim(F.col(q('User')).cast('string')).alias('user_id'),
    F.trim(F.col(q('Card')).cast('string')).alias('card_id'),
    transaction_time.alias('transaction_time'),
    F.regexp_replace(F.col(q('Amount')).cast('string'), r'[$,]', '').cast('decimal(18,2)').alias('amount'),
    F.trim(F.col(q('Use Chip')).cast('string')).alias('use_chip'),
    F.trim(F.col(q('Merchant Name')).cast('string')).alias('merchant_id'),
    F.trim(F.col(q('Merchant City')).cast('string')).alias('merchant_city'),
    F.trim(F.col(q('Merchant State')).cast('string')).alias('merchant_state'),
    F.trim(F.col(q('Zip')).cast('string')).alias('zip_code'),
    F.trim(F.col(q('MCC')).cast('string')).alias('mcc'),
    F.trim(F.col(q('Errors')).cast('string')).alias('Errors'),
    label.alias('_evaluation_label')
).withColumn('card_key', F.when(
    F.col('user_id').isNotNull() & F.col('card_id').isNotNull(),
    F.concat_ws('::', F.col('user_id'), F.col('card_id'))
))

if MAX_ROWS is not None:
    assert isinstance(MAX_ROWS, int) and MAX_ROWS > 0
    prepared = prepared.orderBy(F.col('transaction_id').cast('long')).limit(MAX_ROWS)

required = ['user_id','card_id','merchant_id']
valid_condition = F.col('transaction_time').isNotNull() & F.col('amount').isNotNull()
for c in required:
    valid_condition = valid_condition & F.col(c).isNotNull() & (F.length(F.col(c)) > 0)
valid = prepared.filter(F.coalesce(valid_condition, F.lit(False)))
rejected = prepared.filter(~F.coalesce(valid_condition, F.lit(False)))

# COMMAND ----------

# DBTITLE 1,Write clean, label and rejected tables
# Exclude Is_Fraud from clean, Genie, and custom MCP data access.
# Evaluation labels are written separately and require restricted Unity Catalog grants.
clean_cols = ['transaction_id','user_id','card_id','card_key','transaction_time','amount',
              'use_chip','merchant_id','merchant_city','merchant_state','zip_code','mcc','Errors']
clean_out = (valid.select(*clean_cols)
             .withColumn('source_file', F.lit(SOURCE_TABLE))
             .withColumn('source_row_number', F.lit(None).cast('long')))
label_out = valid.select('transaction_id', F.col('_evaluation_label').alias('Is_Fraud'))
reject_out = rejected.select('transaction_id','user_id','card_id','merchant_id','transaction_time','amount')

outputs = [(CLEAN_TABLE, clean_out), (LABEL_TABLE, label_out), (REJECT_TABLE, reject_out)]
for table, frame in outputs:
    if spark.catalog.tableExists(table) and not REBUILD_DERIVED_TABLES:
        raise RuntimeError(f'{table} already exists. Review it, or explicitly set REBUILD_DERIVED_TABLES=True to rebuild derived tables.')

for table, frame in outputs:
    (frame.write.format('delta').mode('overwrite' if REBUILD_DERIVED_TABLES else 'error')
     .option('overwriteSchema','true').saveAsTable(table))

# COMMAND ----------

source_rows = prepared.count()
clean_rows = spark.table(CLEAN_TABLE).count()
rejected_rows = spark.table(REJECT_TABLE).count()
label_rows = spark.table(LABEL_TABLE).count()
assert source_rows == clean_rows + rejected_rows
assert label_rows == clean_rows
assert spark.table(CLEAN_TABLE).select('transaction_id').distinct().count() == clean_rows
assert 'Is_Fraud' not in spark.table(CLEAN_TABLE).columns
print('PASS: source/sample = clean + rejected:', source_rows, clean_rows, rejected_rows)
print('PASS: unique transaction IDs and evaluation labels separated')
print('IMPORTANT: Restrict SELECT on', LABEL_TABLE, 'to authorized evaluators only.')