#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re, os, sys

FICHIER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'portail_client.html')
if not os.path.exists(FICHIER): print('Fichier introuvable'); sys.exit(1)
with open(FICHIER,'r',encoding='utf-8') as f: c=f.read()

# Extraire le grand script
s=e=None
for m in re.finditer(r'<script(?:\s[^>]*)?>',c):
    en=c.find('</script>',m.start())
    if en-m.start()-len(m.group())>100000:
        s,e=m.start()+len(m.group()),en; break
if s is None: print('Script non trouve'); sys.exit(1)
script=c[s:e]

def patch(old,new,name,script):
    if old in script: print(f'OK: {name}'); return script.replace(old,new,1)
    print(f'SKIP: {name}'); return script

CUVEE_FNS = "\n// -- Cuvees parcellaires (BDD)\nasync function cuveesCharger(){\n if(typeof API_BASE==='undefined') return;\n try{\n  const r=await fetch(`${API_BASE}/cuvees-parcellaires`);\n  const rows=await r.json();\n  if(!Array.isArray(rows)) return;\n  window._cuveesParc=rows.map(row=>({\n   _id:row.id,nom:row.nom,destination:row.destination||'',\n   date_recolte:row.date_recolte||null,\n   parcelles:(row.parcelles||[]).map(p=>({...p,nom_cuvee:row.nom,is_cuvee:true}))\n  }));\n }catch(e){console.error('[CUVEES]',e);}\n}\nasync function cuveesSauvegarder(cuv){\n if(typeof API_BASE==='undefined') return null;\n try{\n  const body={nom:cuv.nom,destination:cuv.destination||'',\n   date_recolte:cuv.date_recolte||null,parcelles:cuv.parcelles,saison:2026};\n  if(cuv._id) body.id=cuv._id;\n  const r=await fetch(`${API_BASE}/cuvees-parcellaires`,{method:'POST',\n   headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});\n  const d=await r.json(); return d.id||null;\n }catch(e){return null;}\n}\nasync function cuveesSupprimer(cid,idx){\n if(!confirm('Supprimer cette cuvee ?')) return;\n if(typeof API_BASE!=='undefined'&&cid)\n  await fetch(`${API_BASE}/cuvees-parcellaires/${cid}`,{method:'DELETE'});\n if(window._cuveesParc) window._cuveesParc.splice(idx,1);\n _itinOuvrirModalDirect();\n}\nfunction cuveesModifier(idx){\n const cuv=window._cuveesParc?.[idx];\n if(!cuv) return;\n _ouvrirAjoutCuvee(cuv,idx);\n}\n"
if 'async function cuveesCharger()' not in script:
    script=patch('function _ouvrirAjoutCuvee(',CUVEE_FNS+'\nfunction _ouvrirAjoutCuvee(','1-CRUD cuvees',script)

script=patch('function _ouvrirAjoutCuvee(){\n const lastF={}',
    'function _ouvrirAjoutCuvee(cuveeExistante, cuveeIdx){\n const lastF={}','2-parametre',script)

if 'await cuveesSauvegarder' not in script:
    old3=("if(!window._cuveesParc)window._cuveesParc=[];\n"
          " window._cuveesParc.push({nom,destination:dest,date_recolte:dateRecolte,parcelles:parcellesMarquees});\n"
          " _itinOuvrirModalDirect();")
    new3=("if(!window._cuveesParc)window._cuveesParc=[];\n"
          " const cuveeObj={nom,destination:dest,date_recolte:dateRecolte,parcelles:parcellesMarquees};\n"
          " const idxEdit=document.getElementById('modalContent')?.dataset?.cuveeIdx;\n"
          " if(idxEdit!==''&&idxEdit!=null&&!isNaN(parseInt(idxEdit))){\n"
          "  const idx2=parseInt(idxEdit);\n"
          "  cuveeObj._id=window._cuveesParc[idx2]?._id||null;\n"
          "  window._cuveesParc[idx2]=cuveeObj;\n"
          " } else { window._cuveesParc.push(cuveeObj); }\n"
          " const newId=await cuveesSauvegarder(cuveeObj);\n"
          " if(newId) cuveeObj._id=newId;\n"
          " _itinOuvrirModalDirect();")
    script=patch(old3,new3,'3-validerCuvee BDD',script)
    script=script.replace('function _validerCuvee(){','async function _validerCuvee(){',1)

script=patch("TAB_INIT.itineraire = ()=>itinChargerDonnees();",
    "TAB_INIT.itineraire = ()=>{itinChargerDonnees();cuveesCharger();};",
    '4-TAB_INIT',script)

script=patch(
    '    h+=`<div style="font-size:12px"><strong>${p.nom}</strong>${src}${avTag}<span style="color:var(--gr);font-size:11px"> ${p.commune}${deg}${deStr}</span></div>`;',
    '    const _nomAffiche=p.nom_cuvee?`${p.nom} -- ${p.nom_cuvee}`:p.nom;\n    h+=`<div style="font-size:12px"><strong>${_nomAffiche}</strong>${src}${avTag}<span style="color:var(--gr);font-size:11px"> ${p.commune}${deg}${deStr}</span></div>`;',
    '5-nomenclature',script)

script=patch(
    "    file2.sort((a,b)=>{\n      const da=_dateCibleCep[a.cepage]||'9999', db_=_dateCibleCep[b.cepage]||'9999';",
    "    file2.sort((a,b)=>{\n      if(a.is_cuvee&&!b.is_cuvee) return -1;\n      if(!a.is_cuvee&&b.is_cuvee) return 1;\n      const da=_dateCibleCep[a.cepage]||'9999', db_=_dateCibleCep[b.cepage]||'9999';",
    '6-sort cuvees premier',script)

if '_datesCiblesEnCours' not in script:
    script=patch('let _itinData = null;',
        'let _itinData = null;\nvar _datesCiblesEnCours = {};','7a-variable dates',script)
    script=script.replace(
        '  if(el&&el.value)datesCibles[cep]=el.value;',
        '  if(el&&el.value){datesCibles[cep]=el.value;_datesCiblesEnCours[cep]=el.value;}')
    print('OK: 7b-sauvegarde dates')
    old_ri=" _ouvrirModal(objectif);\n}"
    new_ri=(" _ouvrirModal(objectif);\n"
             " setTimeout(()=>{\n"
             "  Object.entries(_datesCiblesEnCours).forEach(([cep,dt])=>{\n"
             "   const el=document.getElementById('date-cep-'+cep.replace(/ /g,'-'));\n"
             "   if(el&&dt) el.value=dt;\n"
             "  });\n"
             " },100);\n"
             "}")
    script=patch(old_ri,new_ri,'7c-reinjection dates',script)

c=c[:s]+script+c[e:]
with open(FICHIER,'w',encoding='utf-8') as f: f.write(c)
print('\nFichier mis a jour !')
print('Redemarrez Flask + Ctrl+Shift+R dans le navigateur')