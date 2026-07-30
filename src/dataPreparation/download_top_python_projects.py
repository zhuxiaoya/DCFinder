import requests
import json
import os
import time

# GitHub API URL for searching repositories
url = "https://api.github.com/search/repositories"
download_dir = "top_python_projects"  # 指定下载目录

# Personal access token (replace with your token)
headers = {
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "Python Script"
}

# Parameters for the API request
params = {
    "q": "language:python",  # Filter by Python language
    "sort": "stars",  # Sort by stars
    "order": "desc",  # Order in descending order
    "per_page": 100,  # Number of results per page
    "page": 2  # Starting page
}

# List to store the collected repositories
repositories = []

# Collect repositories page by page
while len(repositories) < 2000 and params["page"] <= 10:  # GitHub API limits to 100 pages
    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        data = response.json()
        repositories.extend(data["items"])
        params["page"] += 1
    else:
        print(f"Failed to retrieve page {params['page']}: Status code {response.status_code}")
        break

    # Add a delay between requests to avoid hitting rate limits
    # time.sleep(5)

# Extract project names and HTML URLs
project_info = [f"{repo['full_name']}: {repo['html_url']}" for repo in repositories]

# Save project information to a text file
with open("top_python_projects.txt", "a") as f:
    for info in project_info:
        f.write(info + "\n")

print(f"Extracted {len(project_info)} project names and URLs and saved to top_python_projects.txt")

# Download each repository using wget

os.makedirs(download_dir, exist_ok=True)

for repo in repositories:
    try:
        # Construct the URL for the ZIP archive of the repository
        zip_url = repo['html_url'] + "/archive/refs/heads/main.zip"
        project_name = repo['full_name'].replace('/', '_')
        download_path = os.path.join(download_dir, f"{project_name}.zip")

        # Use wget to download the ZIP file
        os.system(f"wget -O {download_path} {zip_url}")
        print(f"Downloaded: {project_name}")
    except Exception as e:
        print(f"Failed to download {repo['full_name']}: {e}")

print(f"Downloaded {len(repositories)} repositories to {download_dir}")
