-- Rôle utilisé par l'application. IMPORTANT : ni superuser, ni BYPASSRLS,
-- sinon la Row-Level Security serait ignorée.
CREATE ROLE agent24_app LOGIN PASSWORD 'app_pw'
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

-- Base séparée pour les tests automatiques (ne jamais lancer les tests sur la base de dev).
CREATE DATABASE agent24_test OWNER agent24_owner;
