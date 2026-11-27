-- Task #718: the per-project "AI Skills" page is now "App AI features" (it shows the AI steps found in the app's code), and "Skills"
-- is the new page of build skills. A saved tab layout stored the old page under the key `skills`: move it to `ai_features`, with its
-- rename (labels) and its default-tab mark, so a project's bar keeps showing the same page. Idempotent.
UPDATE project_tabs
   SET tabs = array_replace(tabs, 'skills', 'ai_features'),
       labels = CASE WHEN labels ? 'skills' THEN (labels - 'skills') || jsonb_build_object('ai_features', labels -> 'skills') ELSE labels END,
       default_tab = CASE WHEN default_tab = 'skills' THEN 'ai_features' ELSE default_tab END
 WHERE 'skills' = ANY(tabs) OR labels ? 'skills' OR default_tab = 'skills';
