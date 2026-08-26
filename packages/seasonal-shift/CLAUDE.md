# Release Process

To release a new version (patch/minor/major):

1. **Bump version**
   ```
   poetry version <patch|minor|major>
   ```

2. **Build and publish to PyPI**
   ```
   poetry build && poetry publish
   ```

3. **Update Dockerfile** — change the pinned version in `RUN pip install --no-cache-dir seasonal-shift==<version>`

4. **Commit, tag, and push to GitHub**
   ```
   git add pyproject.toml Dockerfile
   git commit -m "chore: bump to version <version>"
   git tag v<version>
   git push && git push origin v<version>
   ```

5. **Build and push Docker image**
   ```
   docker build --no-cache -t youssefaswad/seasonal-shift:<version> -t youssefaswad/seasonal-shift:latest .
   docker push youssefaswad/seasonal-shift:<version>
   docker push youssefaswad/seasonal-shift:latest
   ```
