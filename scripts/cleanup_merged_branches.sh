#!/bin/bash

# Ensure we're up to date with the remote
echo "Fetching latest changes from remote..."
git fetch origin

# Try to automatically determine the main remote and branch
main_remote=""
main_branch=""

# Check if upstream exists
if git remote | grep -q "^upstream$"; then
    # Check if upstream has a main branch
    if git ls-remote --heads upstream main >/dev/null; then
        main_remote="upstream"
        main_branch="main"
    elif git ls-remote --heads upstream master >/dev/null; then
        main_remote="upstream"
        main_branch="master"
    fi
fi

# If we couldn't find upstream with main/master, prompt user
if [ -z "$main_remote" ]; then
    echo "Please enter the name of the remote for the main repository (e.g., origin or upstream):"
    read main_remote
fi

if [ -z "$main_branch" ]; then
    echo "Please enter the name of the main branch (e.g., main or master):"
    read main_branch
fi

# Update local reference to remote main branch
echo "Using $main_remote/$main_branch as the reference branch"
git fetch $main_remote $main_branch

# Get all local branches
branches=$(git branch | grep -v "^\*" | sed 's/^[[:space:]]*//')

# Counter for deleted branches
deleted=0

echo "Checking branches that can be safely deleted..."

for branch in $branches; do
    # Skip the main branch itself
    if [ "$branch" = "$main_branch" ]; then
        continue
    fi
    
    # Check if all commits in this branch are contained in the main branch
    if git merge-base --is-ancestor "$branch" "$main_remote/$main_branch"; then
        echo "Branch '$branch' is fully merged into $main_remote/$main_branch"
        read -p "Delete this branch? (y/n): " confirm
        if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
            git branch -D "$branch"
            ((deleted++))
            echo "Deleted branch: $branch"
        else
            echo "Skipped branch: $branch"
        fi
    fi
done

echo "Cleanup complete. Deleted $deleted branches."