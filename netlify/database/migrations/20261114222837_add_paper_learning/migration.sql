CREATE TABLE "learning_control" (
	"id" text PRIMARY KEY,
	"enabled" boolean DEFAULT false NOT NULL,
	"data" jsonb NOT NULL,
	"lease_until" timestamp with time zone,
	"lease_owner" text
);
--> statement-breakpoint
CREATE TABLE "paper_runs" (
	"mint" text PRIMARY KEY,
	"data" jsonb NOT NULL,
	"status" text DEFAULT 'open' NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
