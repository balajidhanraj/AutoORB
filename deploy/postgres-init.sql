-- Initialize development database alongside POSTGRES_DB (autoorb_prod)
SELECT 'CREATE DATABASE autoorb_dev'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'autoorb_dev')\gexec
