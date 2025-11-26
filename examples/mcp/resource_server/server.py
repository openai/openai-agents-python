from mcp.server.fastmcp import FastMCP

# Create server
mcp = FastMCP("Resource Server")


# Sample resources providing context data
@mcp.resource("config://app/settings")
def get_app_settings() -> str:
    """Application configuration settings"""
    return """# Application Settings

Server Configuration:
- Host: 0.0.0.0
- Port: 8080
- Environment: production
- Max connections: 1000

Database Configuration:
- Type: PostgreSQL
- Host: db.example.com
- Port: 5432
- Database: myapp_db
- Connection pool size: 20

Cache Configuration:
- Type: Redis
- Host: cache.example.com
- Port: 6379
- TTL: 3600 seconds
"""


@mcp.resource("docs://api/overview")
def get_api_overview() -> str:
    """API documentation overview"""
    return """# API Overview

This API provides access to the core application functionality.

## Authentication
All API requests require a valid API key in the Authorization header:
```
Authorization: Bearer YOUR_API_KEY
```

## Endpoints

### Users API
- GET /api/v1/users - List all users
- GET /api/v1/users/{id} - Get user by ID
- POST /api/v1/users - Create new user
- PUT /api/v1/users/{id} - Update user
- DELETE /api/v1/users/{id} - Delete user

### Products API
- GET /api/v1/products - List all products
- GET /api/v1/products/{id} - Get product by ID
- POST /api/v1/products - Create new product
- PUT /api/v1/products/{id} - Update product
- DELETE /api/v1/products/{id} - Delete product

## Rate Limits
- 1000 requests per hour per API key
- 100 requests per minute per API key

## Error Codes
- 400: Bad Request
- 401: Unauthorized
- 403: Forbidden
- 404: Not Found
- 429: Too Many Requests
- 500: Internal Server Error
"""


@mcp.resource("data://metrics/summary")
def get_metrics_summary() -> str:
    """System metrics summary"""
    return """# System Metrics Summary

Last Updated: 2025-11-26 12:00:00 UTC

## Performance Metrics
- Average Response Time: 45ms
- 95th Percentile Response Time: 120ms
- 99th Percentile Response Time: 250ms
- Requests per Second: 1,250
- Error Rate: 0.02%

## Resource Usage
- CPU Usage: 35%
- Memory Usage: 4.2 GB / 16 GB (26%)
- Disk Usage: 120 GB / 500 GB (24%)
- Network I/O: 15 MB/s

## Database Metrics
- Active Connections: 45 / 100
- Query Response Time (avg): 12ms
- Slow Queries (>1s): 3 in last hour
- Cache Hit Rate: 94%

## User Activity
- Active Users (last hour): 1,450
- New Registrations (today): 87
- Total Users: 125,340
"""


@mcp.resource("docs://security/guidelines")
def get_security_guidelines() -> str:
    """Security best practices and guidelines"""
    return """# Security Guidelines

## Authentication & Authorization
1. Always use strong passwords (min 12 characters)
2. Enable 2FA for all accounts
3. Use role-based access control (RBAC)
4. Implement proper session management
5. Rotate API keys every 90 days

## Data Protection
1. Encrypt sensitive data at rest
2. Use TLS 1.3 for data in transit
3. Implement proper input validation
4. Sanitize all user inputs
5. Follow principle of least privilege

## API Security
1. Rate limit all endpoints
2. Validate all API requests
3. Use HMAC signatures for webhooks
4. Implement CORS properly
5. Log all security events

## Vulnerability Management
1. Scan dependencies regularly
2. Apply security patches within 7 days
3. Conduct regular security audits
4. Maintain security incident response plan
5. Report vulnerabilities to security@example.com

## Compliance
- GDPR compliant
- SOC 2 Type II certified
- ISO 27001 certified
- PCI DSS compliant (for payment data)
"""


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
