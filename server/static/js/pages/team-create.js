/* TeamFlow · 新建团队 */

import { api } from '../api.js';
import { bootstrap } from '../store.js';
import { navigate } from '../router.js';
import { openDialog, fieldHTML, toast } from '../ui.js';

export function openCreateTeamPage(onCreated = null) {
  return openDialog({
    title: '新建团队',
    okText: '创建',
    body: `
      ${fieldHTML({ label: '团队名称', name: 'name', placeholder: '例如：产品研发团队', maxlength: 80 })}
      ${fieldHTML({ label: '团队标识（可选）', name: 'slug', hint: '留空则自动生成，如 team-2026' })}
      ${fieldHTML({ label: '简介（可选）', name: 'description', type: 'textarea', rows: 3 })}`,
    onSubmit: async (values) => {
      const name = String(values.name || '').trim();
      if (!name) { toast('请填写团队名称', 'err'); return false; }
      const payload = { name };
      const slug = String(values.slug || '').trim();
      if (slug) payload.slug = slug;
      const description = String(values.description || '').trim();
      if (description) payload.description = description;
      const data = await api.post('/api/teams', payload);
      await bootstrap();
      toast('团队已创建');
      if (onCreated) await onCreated(data.team);
      if (data.team && data.team.slug) navigate(`/${data.team.slug}/overview`);
      return true;
    },
  });
}
