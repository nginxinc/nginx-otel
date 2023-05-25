#!/usr/bin/env bash
while getopts a:n:u:d: flag
do
    case "${flag}" in
        a) owner=${OPTARG};;
        n) name=${OPTARG};;
        u) url=${OPTARG};;
    esac
done

echo "Owner: $owner";
echo "Repository Name: $name";
echo "Repository URL: $url";

echo "Renaming repository..."

original_owner="{{REPOSITORY_OWNER}}"
original_name="{{REPOSITORY_NAME}}"
original_url="{{REPOSITORY_URL}}"
for filename in $(git ls-files)
do
    sed -i "s/$original_owner/$owner/g" $filename
    sed -i "s/$original_name/$name/g" $filename
    sed -i "s/$original_url/$url/g" $filename
    echo "Renamed $filename"
done

# This command runs only once on GHA!
rm -rf .github/workflows
