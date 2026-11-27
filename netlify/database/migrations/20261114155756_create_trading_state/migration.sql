CREATE TABLE "candidates" (
	"mint" text PRIMARY KEY,
	"data" jsonb NOT NULL,
	"expires_at" timestamp with time zone NOT NULL
);
--> statement-breakpoint
CREATE TABLE "bot_control" (
	"id" text PRIMARY KEY,
	"enabled" boolean DEFAULT false NOT NULL,
	"settings" jsonb NOT NULL,
	"halted_until" timestamp with time zone,
	"lease_owner" text,
	"lease_until" timestamp with time zone,
	"heartbeat" timestamp with time zone,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "events" (
	"id" serial PRIMARY KEY,
	"kind" text NOT NULL,
	"message" text NOT NULL,
	"data" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "orders" (
	"id" text PRIMARY KEY,
	"mint" text NOT NULL,
	"side" text NOT NULL,
	"status" text NOT NULL,
	"signature" text,
	"data" jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "positions" (
	"mint" text PRIMARY KEY,
	"data" jsonb NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
