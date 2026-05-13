-- Run this in Supabase SQL Editor
-- Creates the subscribers table for Money Picks Arena hub

create table if not exists public.subscribers (
  id                     uuid primary key default gen_random_uuid(),
  email                  text unique not null,
  password_hash          text not null,
  stripe_customer_id     text,
  stripe_subscription_id text,
  is_active              boolean default false,
  created_at             timestamp with time zone default now()
);

-- Index for fast email lookups
create index if not exists subscribers_email_idx on public.subscribers (email);
create index if not exists subscribers_stripe_sub_idx on public.subscribers (stripe_subscription_id);
