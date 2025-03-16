import os
from openai import OpenAI
from firecrawl import FirecrawlApp

# Set your API keys here or use environment variables
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "your_openai_api_key")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "your_firecrawl_api_key")

# Initialize clients
openai_client = OpenAI(api_key=OPENAI_API_KEY)
firecrawl_client = FirecrawlApp(api_key=FIRECRAWL_API_KEY)

def extract_url_from_prompt(prompt):
    """Extract a URL from the user prompt."""
    response = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Extract the website URL from the user's prompt. Return only the URL, nothing else."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.1,
    )
    url = response.choices[0].message.content.strip()
    
    # Add https:// if missing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
        
    return url

def main():
    # Check if API keys are set
    if OPENAI_API_KEY == "your_openai_api_key":
        print("Please set your OpenAI API key as an environment variable or in the script.")
        return
    
    if FIRECRAWL_API_KEY == "your_firecrawl_api_key":
        print("Please set your Firecrawl API key as an environment variable or in the script.")
        return
    
    # Get user prompt
    user_prompt = input("Enter your prompt (e.g., 'Extract pricing information from mendable.ai'): ")
    
    # Extract URL from prompt
    url = extract_url_from_prompt(user_prompt)
    print(f"\nScraping {url}...")
    
    # Scrape the website
    try:
        scrape_result = firecrawl_client.scrape_url(url, params={
            "formats": ["markdown"],
            "onlyMainContent": True
        })
    except Exception as e:
        print(f"Error scraping website: {e}")
        return
    
    # Extract content
    if "markdown" in scrape_result:
        content = scrape_result["markdown"]
    else:
        print("No content found in scrape result.")
        return
    
    # Process with OpenAI
    print("Extracting information...")
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that extracts specific information from website content."},
            {"role": "user", "content": f"Based on the following website content, {user_prompt}\n\nContent:\n{content}"}
        ],
        temperature=0.2,
    )
    
    # Print result
    print("\n--- Result ---\n")
    print(response.choices[0].message.content)

if __name__ == "__main__":
    main() 