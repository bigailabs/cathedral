-- Add public flag for deployments with custom subdomains
ALTER TABLE user_deployments
ADD COLUMN IF NOT EXISTS public BOOLEAN NOT NULL DEFAULT FALSE;

-- Index for filtering public deployments
CREATE INDEX IF NOT EXISTS idx_user_deployments_public ON user_deployments(public)
WHERE public = TRUE;

-- Add comment to explain the column
COMMENT ON COLUMN user_deployments.public IS 'Whether this deployment has a public subdomain (e.g., {uuid}.deployments.basilica.ai)';
