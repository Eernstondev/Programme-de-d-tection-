-- Historique des événements pour le tableau de bord web.
-- Un compte Supabase par site client : l'appareil sur place et le client se connectent avec ce compte.

create table if not exists public.events (
  id            uuid primary key,
  user_id       uuid not null default auth.uid() references auth.users (id) on delete cascade,
  ts            timestamptz not null,
  camera        text not null,
  zone          text not null,
  category      text not null check (category in ('personne', 'vehicule', 'animal')),
  labels        text[] not null default '{}',
  count         integer not null default 1,
  plate         text,
  snapshot_path text,
  clip_path     text,
  created_at    timestamptz not null default now()
);

create index if not exists events_user_ts_idx on public.events (user_id, ts desc);
create index if not exists events_user_camera_idx on public.events (user_id, camera, ts desc);

alter table public.events enable row level security;

create policy "events_select_own" on public.events
  for select to authenticated using ((select auth.uid()) = user_id);
create policy "events_insert_own" on public.events
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy "events_update_own" on public.events
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy "events_delete_own" on public.events
  for delete to authenticated using ((select auth.uid()) = user_id);

-- Bucket privé : chaque compte n'accède qu'à son dossier <user_id>/...
insert into storage.buckets (id, name, public)
values ('captures', 'captures', false)
on conflict (id) do nothing;

create policy "captures_own_folder" on storage.objects
  for all to authenticated
  using (bucket_id = 'captures' and (storage.foldername(name))[1] = (select auth.uid())::text)
  with check (bucket_id = 'captures' and (storage.foldername(name))[1] = (select auth.uid())::text);
