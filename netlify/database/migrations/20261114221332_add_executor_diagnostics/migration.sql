ALTER TABLE "bot_control" ADD COLUMN "scheduler_at" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "bot_control" ADD COLUMN "dispatch_at" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "bot_control" ADD COLUMN "executor_error" text;