#!/usr/bin/env sh
# Register the server in Claude Code (user scope, available in every project).
claude mcp add google-ecommerce --scope user \
  -e GA4_PROPERTY_ID=123456789 \
  -e GSC_SITE_URL=sc-domain:example.com \
  -e MERCHANT_ACCOUNT_ID=1234567890 \
  -e GTM_CONTAINER_ID=GTM-XXXXXXX \
  -- uvx google-ecommerce-mcp
