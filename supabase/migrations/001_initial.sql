-- ══════════════════════════════════════════════════════════════════════════
-- Portal Pi v2 — Supabase Initial Migration
-- Ejecutar en SQL Editor del proyecto Supabase
-- ══════════════════════════════════════════════════════════════════════════

-- ─── TABLAS DE DATOS (compartidas — todos los usuarios autenticados) ─────

CREATE TABLE IF NOT EXISTS raw_news (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filename TEXT NOT NULL,
    content TEXT NOT NULL,
    checksum TEXT NOT NULL UNIQUE,
    source TEXT DEFAULT '',
    category TEXT DEFAULT '',
    title TEXT DEFAULT '',
    link TEXT DEFAULT '',
    link_type TEXT DEFAULT '',
    published TEXT DEFAULT '',
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS entities (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    confidence REAL,
    mentions JSONB DEFAULT '[]',
    source_file TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS relations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    source_file TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS syntheses (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    executive_summary TEXT NOT NULL,
    priority TEXT,
    trends JSONB DEFAULT '[]',
    source_files JSONB DEFAULT '[]',
    output_filename TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS classifications (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    primary_category TEXT NOT NULL,
    secondary_tags JSONB DEFAULT '[]',
    justification TEXT DEFAULT '',
    source_file TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS action_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    item_id TEXT,
    description TEXT NOT NULL,
    owner TEXT DEFAULT '',
    deadline TEXT DEFAULT '',
    priority TEXT DEFAULT '',
    source_synthesis TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feed_configs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    category TEXT DEFAULT 'Otro',
    enabled BOOLEAN DEFAULT true,
    poll_interval_min INT DEFAULT 30,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── TABLAS POR USUARIO ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    display_name TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_credentials (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    api_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, provider)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    context_files JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── ÍNDICES ─────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_raw_news_ingested_at ON raw_news(ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_news_source ON raw_news(source);
CREATE INDEX IF NOT EXISTS idx_raw_news_category ON raw_news(category);
CREATE INDEX IF NOT EXISTS idx_raw_news_title ON raw_news USING gin(to_tsvector('simple', title));
CREATE INDEX IF NOT EXISTS idx_raw_news_unprocessed ON raw_news(processed_at) WHERE processed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_created_at ON entities(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_relations_created_at ON relations(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_syntheses_created_at ON syntheses(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_classifications_created_at ON classifications(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_items_created_at ON action_items(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_feed_configs_enabled ON feed_configs(enabled);
CREATE INDEX IF NOT EXISTS idx_user_credentials_user_id ON user_credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_user_id ON chat_messages(user_id, created_at DESC);

-- ─── ROW LEVEL SECURITY ──────────────────────────────────────────────────

ALTER TABLE raw_news ENABLE ROW LEVEL SECURITY;
ALTER TABLE entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE relations ENABLE ROW LEVEL SECURITY;
ALTER TABLE syntheses ENABLE ROW LEVEL SECURITY;
ALTER TABLE classifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE action_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE feed_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;

-- Tablas compartidas: todos los usuarios autenticados pueden leer
-- El backend usa service_role (bypassea RLS) para escritura
CREATE POLICY "Authenticated users can read raw_news"
    ON raw_news FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated users can read entities"
    ON entities FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated users can read relations"
    ON relations FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated users can read syntheses"
    ON syntheses FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated users can read classifications"
    ON classifications FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated users can read action_items"
    ON action_items FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated users can read feed_configs"
    ON feed_configs FOR SELECT TO authenticated USING (true);

CREATE POLICY "Authenticated users can read system_state"
    ON system_state FOR SELECT TO authenticated USING (true);

CREATE POLICY "Users can read own profile"
    ON profiles FOR SELECT TO authenticated USING (auth.uid() = id);

CREATE POLICY "Users can update own profile"
    ON profiles FOR UPDATE TO authenticated USING (auth.uid() = id);

-- user_credentials: solo el propietario
CREATE POLICY "Users can read own credentials"
    ON user_credentials FOR SELECT TO authenticated USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own credentials"
    ON user_credentials FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own credentials"
    ON user_credentials FOR UPDATE TO authenticated USING (auth.uid() = user_id);

CREATE POLICY "Users can delete own credentials"
    ON user_credentials FOR DELETE TO authenticated USING (auth.uid() = user_id);

-- chat_messages: solo el propietario
CREATE POLICY "Users can read own chat_messages"
    ON chat_messages FOR SELECT TO authenticated USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own chat_messages"
    ON chat_messages FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can delete own chat_messages"
    ON chat_messages FOR DELETE TO authenticated USING (auth.uid() = user_id);

-- ─── TRIGGER: auto-crear perfil al registrar usuario ─────────────────────

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.profiles (id, display_name)
    VALUES (
        NEW.id,
        COALESCE(NEW.raw_user_meta_data->>'display_name', split_part(NEW.email, '@', 1))
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE OR REPLACE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- ─── STORAGE BUCKETS ─────────────────────────────────────────────────────

INSERT INTO storage.buckets (id, name, public) VALUES ('raw-news', 'raw-news', false) ON CONFLICT DO NOTHING;
INSERT INTO storage.buckets (id, name, public) VALUES ('pipeline-outputs', 'pipeline-outputs', false) ON CONFLICT DO NOTHING;
INSERT INTO storage.buckets (id, name, public) VALUES ('reports', 'reports', false) ON CONFLICT DO NOTHING;
INSERT INTO storage.buckets (id, name, public) VALUES ('timeline', 'timeline', false) ON CONFLICT DO NOTHING;

-- Storage policies: usuarios autenticados pueden leer/escribir en todos los buckets
CREATE POLICY "Authenticated users can read raw-news"
    ON storage.objects FOR SELECT TO authenticated
    USING (bucket_id = 'raw-news');

CREATE POLICY "Authenticated users can upload raw-news"
    ON storage.objects FOR INSERT TO authenticated
    WITH CHECK (bucket_id = 'raw-news');

CREATE POLICY "Authenticated users can read pipeline-outputs"
    ON storage.objects FOR SELECT TO authenticated
    USING (bucket_id = 'pipeline-outputs');

CREATE POLICY "Authenticated users can upload pipeline-outputs"
    ON storage.objects FOR INSERT TO authenticated
    WITH CHECK (bucket_id = 'pipeline-outputs');

CREATE POLICY "Authenticated users can read reports"
    ON storage.objects FOR SELECT TO authenticated
    USING (bucket_id = 'reports');

CREATE POLICY "Authenticated users can upload reports"
    ON storage.objects FOR INSERT TO authenticated
    WITH CHECK (bucket_id = 'reports');

CREATE POLICY "Authenticated users can read timeline"
    ON storage.objects FOR SELECT TO authenticated
    USING (bucket_id = 'timeline');

CREATE POLICY "Authenticated users can upload timeline"
    ON storage.objects FOR INSERT TO authenticated
    WITH CHECK (bucket_id = 'timeline');
