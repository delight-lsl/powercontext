CREATE DATABASE IF NOT EXISTS dify DEFAULT CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS dify_plugin DEFAULT CHARACTER SET utf8mb4;

CREATE USER IF NOT EXISTS 'dify'@'%' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL PRIVILEGES ON dify.* TO 'dify'@'%';
GRANT ALL PRIVILEGES ON dify_plugin.* TO 'dify'@'%';

CREATE TABLE IF NOT EXISTS dify.heartguide_profiles (
    user_id VARCHAR(128) NOT NULL PRIMARY KEY,
    preference TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS dify.heartguide_turns (
    id CHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(128) NOT NULL,
    conversation_id VARCHAR(128) NOT NULL,
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_heartguide_user_conversation (user_id, conversation_id, created_at)
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS dify.heartguide_memory_reads (
    id CHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(128) NOT NULL,
    conversation_id VARCHAR(128) NOT NULL,
    preference_found TINYINT(1) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_heartguide_memory_read_user (user_id, created_at)
) CHARACTER SET utf8mb4;
