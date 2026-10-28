-- 在 SSMS 使用與應用相同的 SQL 登入，選擇相同資料庫後執行。
-- 僅查詢版本及權限；不建表、不改資料、不顯示帳密或主機名稱。
SELECT
    CONVERT(varchar(32), SERVERPROPERTY('ProductVersion')) AS product_version,
    CONVERT(varchar(128), SERVERPROPERTY('Edition')) AS edition;

SELECT
    SCHEMA_NAME() AS default_schema,
    HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DEFINITION') AS can_view_definition;
